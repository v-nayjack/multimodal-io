/**
 * Upload queue that lives outside the panel.
 *
 * Uploads keep running if the panel is closed or another dataset is opened;
 * reopening the panel reattaches to the live queue. Closing or reloading the
 * browser tab stops uploads (the page warns first); re-adding the same files
 * resumes them.
 */
import {
  cancelUpload,
  isAbort,
  runOperator,
  uploadFile,
  UploadStage,
} from "./engine";

export type Status = "queued" | UploadStage | "done" | "error" | "cancelled";

export type Item = {
  id: string;
  file: File;
  status: Status;
  sent: number;
  startedAt?: number;
  error?: string;
  added?: number;
};

export type State = {
  items: Item[];
  running: boolean;
  datasetName: string | null;
  root: string;
  tags: string;
  finished: string | null;
  refresh: number;
};

const ROOT_PREF_KEY = "multimodal-io:upload-root";

let state: State = {
  items: [],
  running: false,
  datasetName: null,
  root: loadRootPref(),
  tags: "",
  finished: null,
  refresh: 0,
};

const listeners = new Set<() => void>();
const controllers = new Map<string, AbortController>();

export function getState(): State {
  return state;
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function setState(patch: Partial<State> | ((s: State) => Partial<State>)) {
  const next = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...next };
  listeners.forEach((l) => l());
}

function updateItem(id: string, patch: Partial<Item>) {
  setState((s) => ({
    items: s.items.map((it) => (it.id === id ? { ...it, ...patch } : it)),
  }));
}

export function setField(
  field: "datasetName" | "root" | "tags",
  value: string
) {
  if (field === "root") {
    saveRootPref(value);
  }
  setState({ [field]: value } as Partial<State>);
}

/** Adds .mcap files to the queue, ignoring ones already listed. */
export function addFiles(files: FileList | File[] | null) {
  if (!files) return;
  const picked = Array.from(files).filter((f) =>
    f.name.toLowerCase().endsWith(".mcap")
  );
  setState((s) => {
    const known = new Set(s.items.map((it) => it.id));
    const next = picked
      .map((file) => ({
        id: `${file.name}:${file.size}:${file.lastModified}`,
        file,
        status: "queued" as Status,
        sent: 0,
      }))
      .filter((it) => !known.has(it.id));
    return { items: [...s.items, ...next], finished: null };
  });
}

/** Removes a file that isn't uploading from the list. */
export function removeItem(id: string) {
  setState((s) => ({
    items: s.items.filter((it) => it.id !== id || isActive(it.status)),
  }));
}

/** Stops the file that is uploading; finished files stay imported. */
export function cancelItem(id: string) {
  const item = state.items.find((it) => it.id === id);
  if (!item || item.status === "importing") return;
  controllers.get(id)?.abort();
  if (!isActive(item.status)) {
    updateItem(id, { status: "cancelled" });
  }
}

/** Stops the current upload and drops everything still waiting. */
export function cancelAll() {
  setState((s) => ({
    items: s.items.map((it) =>
      it.status === "queued" ? { ...it, status: "cancelled" as Status } : it
    ),
  }));
  state.items
    .filter((it) => it.status === "starting" || it.status === "uploading")
    .forEach((it) => controllers.get(it.id)?.abort());
}

/** Uploads every waiting, failed, or cancelled file, one at a time. */
export async function startBatch(currentDataset: string | null, root: string) {
  if (state.running) return;
  const datasetName = (state.datasetName ?? "").trim();
  const tags = state.tags
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

  setState((s) => ({
    running: true,
    finished: null,
    items: s.items.map((it) =>
      it.status === "error" || it.status === "cancelled"
        ? { ...it, status: "queued" as Status, sent: 0, error: undefined }
        : it
    ),
  }));
  window.addEventListener("beforeunload", warnOnUnload);

  let completed = 0;
  // Pick the next waiting file each time, so files added mid-batch are
  // uploaded and removed ones are skipped
  for (
    let item = nextQueued();
    item;
    item = nextQueued()
  ) {
    const current = item;
    const controller = new AbortController();
    controllers.set(current.id, controller);
    updateItem(current.id, { status: "starting", sent: 0, startedAt: Date.now() });
    try {
      const result = await uploadFile(current.file, {
        root,
        datasetName,
        tags: tags.length ? tags : undefined,
        signal: controller.signal,
        onStage: (stage) => updateItem(current.id, { status: stage }),
        onProgress: (sent) => updateItem(current.id, { sent }),
      });
      completed += 1;
      updateItem(current.id, {
        status: "done",
        sent: current.file.size,
        added: result.num_added,
      });
      setState((s) => ({ refresh: s.refresh + 1 }));
    } catch (e: any) {
      if (isAbort(e)) {
        updateItem(current.id, { status: "cancelled" });
        cancelUpload(current.file, root, datasetName);
      } else {
        updateItem(current.id, {
          status: "error",
          error: e?.message ?? String(e),
        });
      }
    } finally {
      controllers.delete(current.id);
    }
  }

  window.removeEventListener("beforeunload", warnOnUnload);
  setState({ running: false, finished: completed > 0 ? datasetName : null });

  if (completed > 0 && datasetName === currentDataset) {
    runOperator("finish_upload_batch", { dataset_name: datasetName }).catch(
      () => undefined
    );
  }
}

export function isActive(status: Status): boolean {
  return (
    status === "starting" || status === "uploading" || status === "importing"
  );
}

function nextQueued(): Item | undefined {
  return state.items.find((it) => it.status === "queued");
}

function warnOnUnload(e: BeforeUnloadEvent) {
  e.preventDefault();
  e.returnValue = "";
}

function loadRootPref(): string {
  try {
    return window.localStorage.getItem(ROOT_PREF_KEY) ?? "";
  } catch {
    return "";
  }
}

function saveRootPref(root: string) {
  try {
    window.localStorage.setItem(ROOT_PREF_KEY, root);
  } catch {
    // ignore
  }
}
