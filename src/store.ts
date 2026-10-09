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
  refreshed?: number;
  sidecar?: boolean;
  fields?: string[];
  warnings?: string[];
  dest?: string;
};

export type State = {
  items: Item[];
  running: boolean;
  datasetName: string | null;
  root: string;
  tags: string;
  fields: string;
  overwrite: boolean;
  finished: string | null;
  refresh: number;
  followedDataset: string | null;
};

const ROOT_PREF_KEY = "multimodal-io:upload-root";

let state: State = {
  items: [],
  running: false,
  datasetName: null,
  root: loadRootPref(),
  tags: "",
  fields: "",
  // Off on every page load, so files are only replaced on purpose
  overwrite: false,
  finished: null,
  refresh: 0,
  followedDataset: null,
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
  field: "datasetName" | "root" | "tags" | "fields",
  value: string
) {
  if (field === "root") {
    saveRootPref(value);
  }
  setState({ [field]: value } as Partial<State>);
}

export function setOverwrite(overwrite: boolean) {
  setState({ overwrite });
}

/** Whether a file is a YAML sidecar rather than an MCAP file. */
export function isSidecar(name: string): boolean {
  return /\.ya?ml$/i.test(name);
}

/**
 * Adds .mcap files and .yaml sidecars to the queue. A file that is already
 * listed is ignored, unless it finished uploading; then it is queued again
 * so it can be re-uploaded (for example with Overwrite turned on).
 */
export function addFiles(files: FileList | File[] | null) {
  if (!files) return;
  const picked = Array.from(files).filter(
    (f) => f.name.toLowerCase().endsWith(".mcap") || isSidecar(f.name)
  );
  setState((s) => {
    const fresh = picked.map((file) => ({
      id: `${file.name}:${file.size}:${file.lastModified}`,
      file,
      status: "queued" as Status,
      sent: 0,
    }));
    // Drop finished rows for files picked again, so they get a fresh row
    const pickedIds = new Set(fresh.map((it) => it.id));
    const kept = s.items.filter(
      (it) => !(it.status === "done" && pickedIds.has(it.id))
    );
    const known = new Set(kept.map((it) => it.id));
    // Uploads go into one folder, so two different files with the same name
    // would replace each other
    const names = new Set(
      kept.filter((it) => it.status !== "done").map((it) => it.file.name)
    );
    const next = fresh
      .filter((it) => !known.has(it.id))
      .map((it) => {
        if (names.has(it.file.name)) {
          return {
            ...it,
            status: "error" as Status,
            error:
              "Another file with this name is already in the list. Upload " +
              "one recording at a time, or use the CLI for folders",
          };
        }
        names.add(it.file.name);
        return it;
      });
    return { items: [...kept, ...next], finished: null };
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
/**
 * Called when the open dataset changes. While idle, the Dataset field goes
 * back to following the open dataset and finished rows are cleared; while
 * uploading, nothing changes so the batch keeps its target.
 */
export function followDataset(currentDataset: string | null) {
  if (state.followedDataset === currentDataset) return;
  if (state.running) {
    setState({ followedDataset: currentDataset });
    return;
  }
  setState((s) => ({
    followedDataset: currentDataset,
    datasetName: null,
    finished: null,
    items: s.items.filter(
      (it) => it.status !== "done" && it.status !== "cancelled"
    ),
  }));
}

export async function startBatch(
  currentDataset: string | null,
  root: string,
  shownDatasetName: string,
  targetDir?: string | null
) {
  if (state.running) return;
  // Use the name the panel shows, which defaults to the open dataset until
  // the user types a different one
  const datasetName = shownDatasetName.trim();
  if (!datasetName) return;
  const tags = state.tags
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  const fields = state.fields
    .split("\n")
    .map((f) => f.trim())
    .filter(Boolean);

  setState((s) => ({
    running: true,
    finished: null,
    datasetName,
    items: s.items.map((it) =>
      (it.status === "error" || it.status === "cancelled") && !isDuplicate(it)
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
    updateItem(current.id, {
      status: "starting",
      sent: 0,
      startedAt: Date.now(),
      dest: targetDir ?? undefined,
    });
    try {
      const result = await uploadFile(current.file, {
        root,
        datasetName,
        tags: tags.length ? tags : undefined,
        overwrite: state.overwrite,
        fields,
        signal: controller.signal,
        onStage: (stage) => updateItem(current.id, { status: stage }),
        onProgress: (sent) => updateItem(current.id, { sent }),
      });
      completed += 1;
      updateItem(current.id, {
        status: "done",
        sent: current.file.size,
        added: result.num_added,
        refreshed: result.num_refreshed,
        sidecar: result.sidecar,
        fields: result.fields,
        warnings: result.warnings,
      });
      setState((s) => ({ refresh: s.refresh + 1 }));
      // Show each new sample right away when uploading into the open dataset
      // (this also replaces an empty dataset's "No samples yet" page with
      // the grid)
      if (datasetName === currentDataset) {
        runOperator("finish_upload_batch", { dataset_name: datasetName }).catch(
          () => undefined
        );
      }
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

}

export function isActive(status: Status): boolean {
  return (
    status === "starting" || status === "uploading" || status === "importing"
  );
}

/** Sidecars go first, so they are in the bucket when their MCAP imports. */
function nextQueued(): Item | undefined {
  const queued = state.items.filter((it) => it.status === "queued");
  return queued.find((it) => isSidecar(it.file.name)) ?? queued[0];
}

function isDuplicate(item: Item): boolean {
  return state.items.some(
    (it) =>
      it !== item &&
      it.file.name === item.file.name &&
      it.status !== "done" &&
      state.items.indexOf(it) < state.items.indexOf(item)
  );
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
