import React, { useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  Alert,
  Box,
  Button,
  LinearProgress,
  MenuItem,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import * as fos from "@fiftyone/state";
import { useRecoilValue } from "recoil";
import { runOperator } from "./engine";
import {
  addFiles,
  cancelAll,
  cancelItem,
  followDataset,
  getState,
  isActive,
  Item,
  removeItem,
  setField,
  startBatch,
  Status,
  subscribe,
} from "./store";

type Info = {
  roots?: string[];
  root?: string | null;
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

/** The upload panel shown next to the sample grid. */
export default function UploadPanel() {
  return <UploadForm />;
}

/**
 * The same upload UI inside the upload dialog, used for empty datasets where
 * FiftyOne shows no grid and therefore can't open panels.
 */
export function UploadDialog() {
  return <UploadForm embedded />;
}

function UploadForm({ embedded = false }: { embedded?: boolean }) {
  const currentDataset = useRecoilValue(fos.datasetName) as string | null;
  const state = useSyncExternalStore(subscribe, getState);
  const { items, running, root, tags, finished, refresh } = state;
  const datasetName = state.datasetName ?? currentDataset ?? "";
  const [info, setInfo] = useState<Info | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const latestRequest = useRef(0);

  // Show where files will go and whether the dataset can take them
  // Follow the open dataset while no upload is running
  useEffect(() => {
    followDataset(currentDataset);
  }, [currentDataset]);

  useEffect(() => {
    const requestId = ++latestRequest.current;
    const timer = setTimeout(() => {
      runOperator<Info>("get_upload_info", {
        dataset_name: datasetName.trim(),
        root,
      })
        .then((next) => {
          // Only the newest lookup may update the form; an older one that
          // finishes late would otherwise overwrite it
          if (requestId !== latestRequest.current) return;
          // A remembered choice that is no longer allowed falls back to the
          // first allowed location
          if (root && next.roots && !next.roots.includes(root)) {
            setField("root", "");
            return;
          }
          setInfo(next);
        })
        .catch((e) => {
          if (requestId !== latestRequest.current) return;
          setInfo({ username: null, target_dir: null, error: e.message });
        });
    }, 400);
    return () => clearTimeout(timer);
  }, [datasetName, root, refresh]);

  const selectedRoot = root || info?.root || "";

  // An empty dataset shows FiftyOne's "No samples yet" page, which only
  // switches to the grid when the page loads. Once the dialog's uploads into
  // the open dataset finish, reload so the new samples appear
  const willReload =
    embedded && !running && !!finished && finished === currentDataset;
  useEffect(() => {
    if (!willReload) return;
    const timer = setTimeout(() => window.location.reload(), 1500);
    return () => clearTimeout(timer);
  }, [willReload]);
  const hasWork = items.some((it) => it.status !== "done");
  const canStart =
    !running &&
    !!selectedRoot &&
    !!datasetName.trim() &&
    !!info?.target_dir &&
    !info?.error &&
    hasWork;
  const pending = items.filter(
    (it) => it.status === "queued" || isActive(it.status)
  ).length;

  return (
    <Box
      sx={
        embedded
          ? { minWidth: { sm: 560 } }
          : { p: 2, height: "100%", overflow: "auto" }
      }
    >
      <Stack spacing={2} sx={{ maxWidth: 900 }}>
        <Box>
          {!embedded && (
            <Typography variant="h6">Upload MCAP files</Typography>
          )}
          <Typography variant="body2" color="text.secondary">
            {embedded
              ? "Files of any size go straight from your browser to the " +
                "bucket, then get imported. Keep this dialog open until your " +
                "uploads finish; the dataset then opens automatically."
              : "Files of any size go straight from your browser to the " +
                "bucket, then get imported. Uploads keep going if you close " +
                "this panel; keep the browser tab open until they finish. " +
                "Interrupted uploads resume when you add the same file again."}
          </Typography>
        </Box>

        <TextField
          id="mmio-upload-root"
          select
          label="Upload to"
          size="small"
          value={selectedRoot}
          onChange={(e) => setField("root", e.target.value)}
          disabled={running || !info?.roots?.length}
          // Draw the list above the upload dialog, not behind it
          SelectProps={{ MenuProps: { style: { zIndex: 99999 } } }}
          helperText={
            (info?.roots?.length ?? 0) > 1
              ? "Allowed locations are set by your admin"
              : " "
          }
        >
          {(info?.roots ?? []).map((r) => (
            <MenuItem key={r} value={r}>
              {r}
            </MenuItem>
          ))}
        </TextField>

        <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
          <TextField
            id="mmio-dataset-name"
            label="Dataset"
            size="small"
            value={datasetName}
            onChange={(e) => setField("datasetName", e.target.value)}
            disabled={running}
            error={!datasetName.trim()}
            helperText={
              !datasetName.trim()
                ? "Enter a dataset name. A new one is created if it doesn't exist"
                : info?.dataset_state ?? " "
            }
            sx={{ flex: 1 }}
          />
          <TextField
            id="mmio-tags"
            label="Tags (optional, comma separated)"
            size="small"
            value={tags}
            onChange={(e) => setField("tags", e.target.value)}
            disabled={running}
            helperText=" "
            sx={{ flex: 1 }}
          />
        </Stack>

        {willReload && (
          <Alert severity="success">Upload finished, opening the dataset...</Alert>
        )}
        {info?.error && <Alert severity="error">{info.error}</Alert>}
        {!info?.error && info?.target_dir && (
          <Typography variant="body2" color="text.secondary">
            Saves to <code>{info.target_dir}/</code>
          </Typography>
        )}

        <Box
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            addFiles(e.dataTransfer.files);
          }}
          sx={{
            border: "2px dashed",
            borderColor: dragging ? "primary.main" : "divider",
            borderRadius: 1,
            p: 3,
            textAlign: "center",
            bgcolor: dragging ? "action.hover" : "transparent",
          }}
        >
          <Typography>Drag &amp; drop .mcap files here</Typography>
          <Typography
            variant="caption"
            color="text.secondary"
            component="div"
            sx={{ mb: 1 }}
          >
            No size limit · files added during an upload join the queue
          </Typography>
          <Button
            variant="outlined"
            size="small"
            onClick={() => inputRef.current?.click()}
          >
            Choose files
          </Button>
          <input
            ref={inputRef}
            type="file"
            accept=".mcap"
            multiple
            hidden
            onChange={(e) => {
              addFiles(e.target.files);
              e.target.value = "";
            }}
          />
        </Box>

        {items.length > 0 && (
          <Stack spacing={1.5}>
            {items.map((item) => (
              <FileRow key={item.id} item={item} />
            ))}
          </Stack>
        )}

        <Stack direction="row" spacing={1}>
          <Button
            variant="contained"
            disabled={!canStart}
            onClick={() =>
              startBatch(
                currentDataset,
                selectedRoot,
                datasetName,
                info?.target_dir
              )
            }
          >
            {running ? `Uploading (${pending} left)...` : "Start upload"}
          </Button>
          {running && (
            <Button variant="outlined" color="error" onClick={cancelAll}>
              Cancel all
            </Button>
          )}
          {!running && finished && finished !== currentDataset && (
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

function FileRow({ item }: { item: Item }) {
  const pct = item.file.size ? (100 * item.sent) / item.file.size : 0;
  const canCancel = item.status === "starting" || item.status === "uploading";
  const canRemove = !isActive(item.status) && item.status !== "done";
  const detail =
    item.status === "uploading"
      ? `${formatBytes(item.sent)} of ${formatBytes(item.file.size)}${eta(item)}`
      : item.status === "done"
      ? `${formatBytes(item.file.size)} · ${
          item.added ? "imported" : "already in dataset"
        }`
      : formatBytes(item.file.size);

  return (
    <Box>
      <Stack
        direction="row"
        justifyContent="space-between"
        alignItems="baseline"
        spacing={1}
      >
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="body2" sx={{ wordBreak: "break-all" }}>
            {item.file.name}
          </Typography>
          {item.dest && (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ wordBreak: "break-all", fontFamily: "monospace" }}
            >
              → {item.dest}/
            </Typography>
          )}
        </Box>
        <Stack direction="row" spacing={1} alignItems="baseline">
          <Typography
            variant="caption"
            color={item.status === "error" ? "error" : "text.secondary"}
            sx={{ whiteSpace: "nowrap" }}
          >
            {STATUS_LABEL[item.status]} · {detail}
          </Typography>
          {canCancel && (
            <Button size="small" onClick={() => cancelItem(item.id)}>
              Cancel
            </Button>
          )}
          {canRemove && (
            <Button size="small" onClick={() => removeItem(item.id)}>
              Remove
            </Button>
          )}
        </Stack>
      </Stack>
      <LinearProgress
        variant={
          item.status === "starting" || item.status === "importing"
            ? "indeterminate"
            : "determinate"
        }
        value={item.status === "done" ? 100 : pct}
        color={
          item.status === "error"
            ? "error"
            : item.status === "done"
            ? "success"
            : "primary"
        }
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
