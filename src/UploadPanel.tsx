import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  LinearProgress,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import * as fos from "@fiftyone/state";
import { useRecoilValue } from "recoil";
import {
  cancelUpload,
  isAbort,
  runOperator,
  uploadFile,
  UploadStage,
} from "./engine";

type Status = "queued" | UploadStage | "done" | "error" | "cancelled";

type Item = {
  id: string;
  file: File;
  status: Status;
  sent: number;
  startedAt?: number;
  error?: string;
  added?: number;
};

type Info = {
  root: string | null;
  username: string | null;
  target_dir: string | null;
  dataset_state?: string;
  error?: string;
};

const STATUS_LABEL: Record<Status, string> = {
  queued: "Waiting",
  starting: "Preparing",
  uploading: "Uploading",
  importing: "Importing",
  done: "Done",
  error: "Failed",
  cancelled: "Cancelled",
};

export default function UploadPanel() {
  const currentDataset = useRecoilValue(fos.datasetName) as string | null;
  const [datasetName, setDatasetName] = useState<string>(currentDataset ?? "");
  const [tags, setTags] = useState("");
  const [info, setInfo] = useState<Info | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [running, setRunning] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [finished, setFinished] = useState<string | null>(null);
  const controllers = useRef(new Map<string, AbortController>());
  const inputRef = useRef<HTMLInputElement>(null);

  // Show where files will go and whether the dataset can take them
  useEffect(() => {
    const timer = setTimeout(() => {
      runOperator<Info>("get_upload_info", {
        dataset_name: datasetName.trim(),
      })
        .then(setInfo)
        .catch((e) =>
          setInfo({
            root: null,
            username: null,
            target_dir: null,
            error: e.message,
          })
        );
    }, 400);
    return () => clearTimeout(timer);
  }, [datasetName]);

  // Warn before closing the tab mid-upload
  useEffect(() => {
    if (!running) return;
    const onUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [running]);

  const update = useCallback((id: string, patch: Partial<Item>) => {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...patch } : it)));
  }, []);

  const addFiles = useCallback((files: FileList | File[] | null) => {
    if (!files) return;
    const picked = Array.from(files).filter((f) =>
      f.name.toLowerCase().endsWith(".mcap")
    );
    setFinished(null);
    setItems((prev) => {
      const known = new Set(prev.map((it) => it.id));
      const next = picked
        .map((file) => ({
          id: `${file.name}:${file.size}:${file.lastModified}`,
          file,
          status: "queued" as Status,
          sent: 0,
        }))
        .filter((it) => !known.has(it.id));
      return [...prev, ...next];
    });
  }, []);

  const start = useCallback(async () => {
    const name = datasetName.trim();
    const tagList = tags
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    setRunning(true);
    setFinished(null);
    let completed = 0;

    for (const item of items) {
      if (item.status === "done") continue;
      const controller = new AbortController();
      controllers.current.set(item.id, controller);
      update(item.id, { status: "starting", sent: 0, error: undefined, startedAt: Date.now() });
      try {
        const result = await uploadFile(item.file, {
          datasetName: name,
          tags: tagList.length ? tagList : undefined,
          signal: controller.signal,
          onStage: (stage) => update(item.id, { status: stage }),
          onProgress: (sent) => update(item.id, { sent }),
        });
        completed += 1;
        update(item.id, { status: "done", sent: item.file.size, added: result.num_added });
      } catch (e: any) {
        update(item.id, isAbort(e) ? { status: "cancelled" } : { status: "error", error: e?.message ?? String(e) });
      } finally {
        controllers.current.delete(item.id);
      }
    }

    setRunning(false);
    if (completed > 0) {
      setFinished(name);
      if (name === currentDataset) {
        runOperator("finish_upload_batch", { dataset_name: name }).catch(() => undefined);
      }
    }
  }, [items, datasetName, tags, update, currentDataset]);

  const cancel = useCallback(
    (item: Item) => {
      controllers.current.get(item.id)?.abort();
      cancelUpload(item.file, datasetName.trim());
      update(item.id, { status: "cancelled" });
    },
    [datasetName, update]
  );

  const removeItem = useCallback((id: string) => {
    setItems((prev) => prev.filter((it) => it.id !== id));
  }, []);

  const canStart =
    !running &&
    !!datasetName.trim() &&
    !!info?.target_dir &&
    !info?.error &&
    items.some((it) => it.status !== "done");

  return (
    <Box sx={{ p: 2, height: "100%", overflow: "auto" }}>
      <Stack spacing={2} sx={{ maxWidth: 900 }}>
        <Box>
          <Typography variant="h6">Upload MCAP files</Typography>
          <Typography variant="body2" color="text.secondary">
            Files of any size go straight from your browser to the bucket, then
            get imported. Keep this tab open until uploads finish. Interrupted
            uploads resume when you add the same file again.
          </Typography>
        </Box>

        <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
          <TextField
            id="mmio-dataset-name"
            label="Dataset"
            size="small"
            value={datasetName}
            onChange={(e) => setDatasetName(e.target.value)}
            disabled={running}
            helperText={info?.dataset_state ?? " "}
            sx={{ flex: 1 }}
          />
          <TextField
            id="mmio-tags"
            label="Tags (optional, comma separated)"
            size="small"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            disabled={running}
            helperText=" "
            sx={{ flex: 1 }}
          />
        </Stack>

        {info?.error && <Alert severity="error">{info.error}</Alert>}
        {!info?.error && info?.target_dir && (
          <Typography variant="body2" color="text.secondary">
            Saves to <code>{info.target_dir}/</code>
          </Typography>
        )}

        <Box
          role="button"
          tabIndex={0}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
          }}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            if (!running) addFiles(e.dataTransfer.files);
          }}
          sx={{
            border: "2px dashed",
            borderColor: dragging ? "primary.main" : "divider",
            borderRadius: 1,
            p: 3,
            textAlign: "center",
            cursor: running ? "default" : "pointer",
            bgcolor: dragging ? "action.hover" : "transparent",
          }}
        >
          <Typography>Drag &amp; drop .mcap files, or click to choose</Typography>
          <Typography variant="caption" color="text.secondary">
            No size limit
          </Typography>
          <input
            ref={inputRef}
            type="file"
            accept=".mcap"
            multiple
            hidden
            disabled={running}
            onChange={(e) => {
              addFiles(e.target.files);
              e.target.value = "";
            }}
          />
        </Box>

        {items.length > 0 && (
          <Stack spacing={1.5}>
            {items.map((item) => (
              <FileRow
                key={item.id}
                item={item}
                running={running}
                onCancel={() => cancel(item)}
                onRemove={() => removeItem(item.id)}
              />
            ))}
          </Stack>
        )}

        <Stack direction="row" spacing={1}>
          <Button variant="contained" disabled={!canStart} onClick={start}>
            {running ? "Uploading..." : "Start upload"}
          </Button>
          {finished && finished !== currentDataset && (
            <Button
              variant="outlined"
              onClick={() =>
                runOperator("finish_upload_batch", { dataset_name: finished })
              }
            >
              Open {finished}
            </Button>
          )}
        </Stack>
      </Stack>
    </Box>
  );
}

function FileRow({
  item,
  running,
  onCancel,
  onRemove,
}: {
  item: Item;
  running: boolean;
  onCancel: () => void;
  onRemove: () => void;
}) {
  const pct = item.file.size ? (100 * item.sent) / item.file.size : 0;
  const active = item.status === "starting" || item.status === "uploading" || item.status === "importing";
  const detail =
    item.status === "uploading"
      ? `${formatBytes(item.sent)} of ${formatBytes(item.file.size)}${eta(item)}`
      : item.status === "done"
      ? `${formatBytes(item.file.size)} · ${item.added ? "imported" : "already in dataset"}`
      : formatBytes(item.file.size);

  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" alignItems="baseline" spacing={1}>
        <Typography variant="body2" sx={{ wordBreak: "break-all" }}>
          {item.file.name}
        </Typography>
        <Stack direction="row" spacing={1} alignItems="baseline">
          <Typography variant="caption" color={item.status === "error" ? "error" : "text.secondary"} sx={{ whiteSpace: "nowrap" }}>
            {STATUS_LABEL[item.status]} · {detail}
          </Typography>
          {active && (
            <Button size="small" onClick={onCancel}>
              Cancel
            </Button>
          )}
          {!running && !active && item.status !== "done" && (
            <Button size="small" onClick={onRemove}>
              Remove
            </Button>
          )}
        </Stack>
      </Stack>
      <LinearProgress
        variant={item.status === "starting" || item.status === "importing" ? "indeterminate" : "determinate"}
        value={item.status === "done" ? 100 : pct}
        color={item.status === "error" ? "error" : item.status === "done" ? "success" : "primary"}
      />
      {item.error && (
        <Typography variant="caption" color="error">
          {item.error}
        </Typography>
      )}
    </Box>
  );
}

function eta(item: Item): string {
  if (!item.startedAt || item.sent <= 0) return "";
  const elapsed = (Date.now() - item.startedAt) / 1000;
  if (elapsed < 3) return "";
  const rate = item.sent / elapsed;
  const remaining = (item.file.size - item.sent) / rate;
  const mins = Math.round(remaining / 60);
  return ` · ${formatBytes(rate)}/s · ${mins < 1 ? "<1" : mins} min left`;
}

function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = bytes;
  let i = 0;
  while (n >= 1000 && i < units.length - 1) {
    n /= 1000;
    i++;
  }
  return i === 0 ? `${n} B` : `${n.toFixed(1)} ${units[i]}`;
}
