/**
 * Browser side of large uploads.
 *
 * The plugin's Python operators hand out signed upload targets for one exact
 * object path; this module sends the file bytes straight to the bucket and
 * never through the FiftyOne server:
 *
 * - S3 / MinIO: multipart upload, several parts in parallel, resumable by part
 * - GCS: resumable upload session, resumable by byte offset
 * - Azure: block blob upload, several blocks in parallel, resumable by block
 *
 * Unfinished uploads are remembered in localStorage, so picking the same file
 * again (even after a page reload) resumes instead of starting over.
 */
import { executeOperator } from "@fiftyone/operators";

export const PLUGIN = "@v-nayjack/multimodal-io";

const PART_CONCURRENCY = 4;
const MAX_RETRIES = 4;
const STORE_PREFIX = "multimodal-io:upload:";

export type UploadStage = "starting" | "uploading" | "importing";

export type UploadResult = {
  dataset: string;
  num_added: number;
  path: string;
};

type Plan = {
  mode:
    | "s3_multipart"
    | "gcs_resumable"
    | "azure_blocks"
    | "single_put"
    | "exists";
  path: string;
  upload_id?: string;
  part_size?: number;
  parts?: { number: number; url: string }[];
  done?: { number: number; etag: string | null }[];
  num_parts?: number;
  session_url?: string;
  chunk_size?: number;
  url?: string;
  headers?: Record<string, string>;
};

type Saved = Pick<Plan, "mode" | "upload_id" | "session_url" | "chunk_size">;

export type UploadOptions = {
  root: string;
  datasetName: string;
  tags?: string[];
  onProgress: (sentBytes: number) => void;
  onStage: (stage: UploadStage) => void;
  signal: AbortSignal;
};

/** Runs one of this plugin's Python operators and returns its result. */
export function runOperator<T = any>(
  name: string,
  params: Record<string, unknown>
): Promise<T> {
  return new Promise((resolve, reject) => {
    executeOperator(`${PLUGIN}/${name}`, params, {
      skipOutput: true,
      skipErrorNotification: true,
      callback: (result: any) => {
        if (result?.error) {
          reject(
            new Error(
              result.errorMessage || String(result.error) || "Operation failed"
            )
          );
        } else {
          resolve((result?.result ?? {}) as T);
        }
      },
    });
  });
}

/** Uploads one file to the user's folder, then imports it. */
export async function uploadFile(
  file: File,
  opts: UploadOptions
): Promise<UploadResult> {
  const base = {
    filename: file.name,
    size: file.size,
    dataset_name: opts.datasetName,
    root: opts.root,
    origin: window.location.origin,
  };
  const key = storeKey(file, opts.root, opts.datasetName);

  opts.onStage("starting");
  const saved = load(key);
  let plan: Plan | null = null;
  let resumedGcs = false;

  if (
    (saved?.mode === "s3_multipart" && saved.upload_id) ||
    saved?.mode === "azure_blocks"
  ) {
    try {
      plan = await runOperator<Plan>("resume_large_upload", {
        ...base,
        mode: saved.mode,
        upload_id: saved.upload_id,
      });
    } catch {
      // The upload expired or was cancelled elsewhere; start over
      remove(key);
    }
  } else if (saved?.mode === "gcs_resumable" && saved.session_url) {
    plan = {
      mode: "gcs_resumable",
      path: "",
      session_url: saved.session_url,
      chunk_size: saved.chunk_size,
    };
    resumedGcs = true;
  }

  if (!plan) {
    plan = await runOperator<Plan>("start_large_upload", base);
  }

  if (plan.mode !== "exists") {
    save(key, {
      mode: plan.mode,
      upload_id: plan.upload_id,
      session_url: plan.session_url,
      chunk_size: plan.chunk_size,
    });
  }

  opts.onStage("uploading");
  let parts: { number: number; etag: string }[] | undefined;

  try {
    if (plan.mode === "s3_multipart") {
      const uploadId = plan.upload_id;
      parts = await uploadS3(file, plan, opts, () =>
        runOperator<Plan>("resume_large_upload", {
          ...base,
          mode: "s3_multipart",
          upload_id: uploadId,
        })
      );
    } else if (plan.mode === "azure_blocks") {
      await uploadAzure(file, plan, opts, () =>
        runOperator<Plan>("resume_large_upload", {
          ...base,
          mode: "azure_blocks",
        })
      );
    } else if (plan.mode === "gcs_resumable") {
      await uploadGcs(file, plan, resumedGcs, opts);
    } else if (plan.mode === "single_put") {
      await withRetries(async () => {
        const xhr = await put(
          plan!.url!,
          file,
          plan!.headers,
          opts.onProgress,
          opts.signal
        );
        assertOk(xhr, "Upload");
      }, opts.signal);
    }
  } catch (e) {
    if (resumedGcs && !isAbort(e)) {
      // Resumable sessions expire after a week; forget it so a retry restarts
      remove(key);
    }
    throw e;
  }

  opts.onProgress(file.size);
  opts.onStage("importing");
  const result = await runOperator<UploadResult>("complete_large_upload", {
    ...base,
    mode: plan.mode,
    upload_id: plan.upload_id,
    parts,
    tags: opts.tags,
  });
  remove(key);
  return result;
}

/** Cancels an in-progress upload and discards what was sent so far. */
export async function cancelUpload(
  file: File,
  root: string,
  datasetName: string
): Promise<void> {
  const key = storeKey(file, root, datasetName);
  const saved = load(key);
  remove(key);
  if (saved?.mode === "s3_multipart" && saved.upload_id) {
    await runOperator("abort_large_upload", {
      filename: file.name,
      size: file.size,
      dataset_name: datasetName,
      root,
      mode: saved.mode,
      upload_id: saved.upload_id,
    }).catch(() => undefined);
  }
}

async function uploadS3(
  file: File,
  plan: Plan,
  opts: UploadOptions,
  refreshPlan: () => Promise<Plan>
): Promise<{ number: number; etag: string }[]> {
  const partSize = plan.part_size!;
  // Signed part links expire; with temporary (role-based) server credentials
  // that can be after about an hour. When a part is refused, fetch fresh
  // links once for all workers and retry
  const urls = new Map<number, string>(
    plan.parts!.map((p) => [p.number, p.url])
  );
  let refreshing: Promise<void> | null = null;
  const refreshUrls = () => {
    if (!refreshing) {
      refreshing = refreshPlan()
        .then((fresh) => {
          fresh.parts?.forEach((p) => urls.set(p.number, p.url));
        })
        .finally(() => {
          refreshing = null;
        });
    }
    return refreshing;
  };
  // S3 always reports an ETag for finished parts
  const done = new Map<number, string>(
    (plan.done ?? []).map((p) => [p.number, p.etag ?? ""])
  );
  const sent = new Map<number, number>();
  const partBytes = (n: number) =>
    Math.min(partSize, file.size - (n - 1) * partSize);

  done.forEach((_, n) => sent.set(n, partBytes(n)));
  const report = () => {
    let total = 0;
    sent.forEach((v) => (total += v));
    opts.onProgress(total);
  };
  report();

  const queue = plan.parts!.filter((p) => !done.has(p.number));
  const worker = async () => {
    for (let part = queue.shift(); part; part = queue.shift()) {
      const current = part;
      const start = (current.number - 1) * partSize;
      const blob = file.slice(start, start + partBytes(current.number));
      const xhr = await withRetries(async () => {
        let x = await put(
          urls.get(current.number)!,
          blob,
          undefined,
          (loaded) => {
            sent.set(current.number, loaded);
            report();
          },
          opts.signal
        );
        if (x.status === 403 || x.status === 400) {
          await refreshUrls();
          x = await put(
            urls.get(current.number)!,
            blob,
            undefined,
            (loaded) => {
              sent.set(current.number, loaded);
              report();
            },
            opts.signal
          );
        }
        assertOk(x, `Part ${current.number}`);
        return x;
      }, opts.signal);

      const etag = xhr.getResponseHeader("ETag");
      if (!etag) {
        throw new Error(
          "The bucket's CORS settings must expose the ETag header for " +
            "uploads (see the plugin README)"
        );
      }
      done.set(current.number, etag);
      sent.set(current.number, blob.size);
      report();
    }
  };

  await Promise.all(
    Array.from({ length: Math.min(PART_CONCURRENCY, queue.length) }, worker)
  );

  return Array.from(done, ([number, etag]) => ({ number, etag }));
}

async function uploadAzure(
  file: File,
  plan: Plan,
  opts: UploadOptions,
  refreshPlan: () => Promise<Plan>
): Promise<void> {
  const blockSize = plan.part_size!;
  const numBlocks = plan.num_parts!;
  // One SAS URL covers every block; refresh it if Azure refuses a block
  let baseUrl = plan.url!;
  let refreshing: Promise<void> | null = null;
  const refreshUrl = () => {
    if (!refreshing) {
      refreshing = refreshPlan()
        .then((fresh) => {
          if (fresh.url) baseUrl = fresh.url;
        })
        .finally(() => {
          refreshing = null;
        });
    }
    return refreshing;
  };

  const done = new Set((plan.done ?? []).map((p) => p.number));
  const sent = new Map<number, number>();
  const blockBytes = (n: number) =>
    Math.min(blockSize, file.size - (n - 1) * blockSize);
  done.forEach((n) => sent.set(n, blockBytes(n)));
  const report = () => {
    let total = 0;
    sent.forEach((v) => (total += v));
    opts.onProgress(total);
  };
  report();

  const queue: number[] = [];
  for (let n = 1; n <= numBlocks; n++) {
    if (!done.has(n)) queue.push(n);
  }

  const worker = async () => {
    for (let n = queue.shift(); n !== undefined; n = queue.shift()) {
      const number = n;
      const start = (number - 1) * blockSize;
      const blob = file.slice(start, start + blockBytes(number));
      const blockUrl = () =>
        `${baseUrl}&comp=block&blockid=${encodeURIComponent(azureBlockId(number))}`;
      await withRetries(async () => {
        const onProgress = (loaded: number) => {
          sent.set(number, loaded);
          report();
        };
        let x = await put(blockUrl(), blob, undefined, onProgress, opts.signal);
        if (x.status === 403) {
          await refreshUrl();
          x = await put(blockUrl(), blob, undefined, onProgress, opts.signal);
        }
        assertOk(x, `Block ${number}`);
      }, opts.signal);
      sent.set(number, blob.size);
      report();
    }
  };

  await Promise.all(
    Array.from({ length: Math.min(PART_CONCURRENCY, queue.length) }, worker)
  );
}

/** Same block IDs as the server: base64 of the zero-padded block number. */
function azureBlockId(number: number): string {
  return btoa(String(number).padStart(6, "0"));
}

async function uploadGcs(
  file: File,
  plan: Plan,
  resumed: boolean,
  opts: UploadOptions
): Promise<void> {
  const url = plan.session_url!;
  const chunkSize = plan.chunk_size!;
  let offset = resumed ? await gcsOffset(url, file.size, opts.signal) : 0;
  let failures = 0;

  while (offset < file.size) {
    const end = Math.min(offset + chunkSize, file.size) - 1;
    const chunkStart = offset;
    try {
      const xhr = await put(
        url,
        file.slice(chunkStart, end + 1),
        { "Content-Range": `bytes ${chunkStart}-${end}/${file.size}` },
        (loaded) => opts.onProgress(chunkStart + loaded),
        opts.signal
      );
      if (xhr.status === 308) {
        offset = nextOffset(xhr.getResponseHeader("Range"));
      } else if (xhr.status === 200 || xhr.status === 201) {
        offset = file.size;
      } else {
        throw new Error(`Upload failed (HTTP ${xhr.status})`);
      }
      failures = 0;
    } catch (e) {
      if (isAbort(e) || ++failures > MAX_RETRIES) {
        throw e;
      }
      await sleep(1000 * 2 ** failures, opts.signal);
      // Ask the session how much it actually received, then continue
      offset = await gcsOffset(url, file.size, opts.signal);
    }
  }
}

async function gcsOffset(
  url: string,
  size: number,
  signal: AbortSignal
): Promise<number> {
  const xhr = await put(
    url,
    null,
    { "Content-Range": `bytes */${size}` },
    undefined,
    signal
  );
  if (xhr.status === 200 || xhr.status === 201) {
    return size;
  }
  if (xhr.status === 308) {
    return nextOffset(xhr.getResponseHeader("Range"));
  }
  throw new Error(
    `Upload session is no longer valid (HTTP ${xhr.status}); retry to start over`
  );
}

function nextOffset(range: string | null): number {
  // "bytes=0-1234" means bytes 0..1234 were received
  const match = range && /bytes=\d+-(\d+)/.exec(range);
  return match ? Number(match[1]) + 1 : 0;
}

function put(
  url: string,
  body: Blob | null,
  headers: Record<string, string> | undefined,
  onProgress: ((loaded: number) => void) | undefined,
  signal: AbortSignal
): Promise<XMLHttpRequest> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError());
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    Object.entries(headers ?? {}).forEach(([k, v]) =>
      xhr.setRequestHeader(k, v)
    );
    if (onProgress) {
      xhr.upload.onprogress = (e) => onProgress(e.loaded);
    }
    const onAbort = () => xhr.abort();
    signal.addEventListener("abort", onAbort, { once: true });
    xhr.onload = () => {
      signal.removeEventListener("abort", onAbort);
      resolve(xhr);
    };
    xhr.onerror = () => {
      signal.removeEventListener("abort", onAbort);
      reject(
        new Error(
          "Network error. If this repeats, check the bucket's CORS settings " +
            "allow PUT from this site"
        )
      );
    };
    xhr.onabort = () => reject(abortError());
    xhr.send(body);
  });
}

async function withRetries<T>(
  fn: () => Promise<T>,
  signal: AbortSignal
): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await fn();
    } catch (e) {
      if (isAbort(e) || attempt >= MAX_RETRIES) {
        throw e;
      }
      await sleep(1000 * 2 ** attempt, signal);
    }
  }
}

function assertOk(xhr: XMLHttpRequest, what: string) {
  if (xhr.status < 200 || xhr.status >= 300) {
    throw new Error(`${what} failed (HTTP ${xhr.status})`);
  }
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(abortError());
      },
      { once: true }
    );
  });
}

function abortError() {
  return new DOMException("Upload cancelled", "AbortError");
}

export function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

function storeKey(file: File, root: string, datasetName: string) {
  return `${STORE_PREFIX}${root}:${datasetName}:${file.name}:${file.size}:${file.lastModified}`;
}

function load(key: string): Saved | null {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as Saved) : null;
  } catch {
    return null;
  }
}

function save(key: string, value: Saved) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage may be unavailable (eg private mode); uploads still work
  }
}

function remove(key: string) {
  try {
    window.localStorage.removeItem(key);
  } catch {
    // ignore
  }
}
