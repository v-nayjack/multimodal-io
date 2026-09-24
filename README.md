## Multimodal I/O Plugin

This plugin brings robotics recordings into FiftyOne Enterprise from any cloud
bucket your deployment can reach (GCS, S3, Azure, MinIO). It supports two
formats:

-   **MCAP**: every `.mcap` file in a folder (including subfolders) becomes
    one sample
-   **LeRobot v3**: a folder containing `meta/info.json` is imported as one
    sample per episode

There are two ways to use it:

| Use case                                      | Tool                                  |
| --------------------------------------------- | ------------------------------------- |
| Browse a bucket and import a folder in place  | `import_multimodal` operator (App)    |
| Quick experiment with one small MCAP file     | `upload_multimodal` operator (App)    |
| Upload a large local folder, then import it   | [`upload.py`](upload.py) script (CLI) |

Both paths share the same format detection and folder rules
([`core.py`](core.py)), so data lands in the same place however it arrives.

Requires FiftyOne Enterprise `>=2.25.0` with multimodal support enabled.

## Installation

An admin installs the plugin from the App (Settings > Plugins), or with the
management SDK:

```py
import fiftyone.management as fom

fom.upload_plugin("/path/to/multimodal-io", overwrite=True)
```

To use it locally:

```shell
fiftyone plugins download https://github.com/v-nayjack/multimodal-io
```

## Configuration

Settings are read from plugin secrets (Settings > Secrets) or environment
variables of the same name. None of them hold credentials.

| Setting                                | Purpose                                                                                     | Default                              |
| -------------------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------ |
| `FIFTYONE_MULTIMODAL_IO_ROOT`          | The bucket folder that imports are limited to and uploads go to, eg `gs://acme/fiftyone`   | unset: browse anything, no uploads |
| `FIFTYONE_MULTIMODAL_IO_PATH_TEMPLATE` | Where uploads land, using `{root}`, `{username}`, `{dataset}`                              | `{root}/users/{username}/{dataset}`  |
| `FIFTYONE_MULTIMODAL_IO_MAX_UPLOAD_MB` | The largest file the App will accept for upload                                             | `100`                                |

### Recommended bucket layout

```
<root>/
├── users/
│   └── <username>/          # from the logged-in user's email
│       └── <dataset>/
│           ├── *.mcap       # or a LeRobot tree
│           └── _import.json # who uploaded what, and when
└── shared/
    └── <dataset>/           # curated, pipeline-owned data
```

Every upload has an owner, experiments stay in each person's folder, and
curated data stays separate. If your team uses a different convention, change
`FIFTYONE_MULTIMODAL_IO_PATH_TEMPLATE`.

### Credentials

Bucket access always goes through the deployment's
[cloud credentials](https://docs.voxel51.com/enterprise/cloud_media.html).
The plugin never asks for, stores, or logs bucket keys.

-   In the App, operators use the credentials the admin configured, so users
    need no bucket access of their own
-   The CLI connects with `FIFTYONE_API_URI` and `FIFTYONE_API_KEY`. In this
    mode, the SDK fetches the deployment's cloud credentials, so users only
    need an API key. Local credentials (eg `AWS_*` environment variables) are
    used if present

### Bucket CORS

The App reads MCAP and LeRobot files straight from the bucket using HTTP range
requests, so the bucket's CORS policy must allow your deployment's origin and
**expose** the range headers. Without `ExposeHeaders`, imports succeed but
samples fail to open with
`Failed to read recording: Expected Content-Range header for byte-range response`.

S3 example:

```json
[
    {
        "AllowedOrigins": ["https://your-deployment.fiftyone.ai"],
        "AllowedMethods": ["GET", "HEAD"],
        "AllowedHeaders": ["*"],
        "ExposeHeaders": [
            "Content-Range",
            "Content-Length",
            "Accept-Ranges",
            "Content-Type"
        ],
        "MaxAgeSeconds": 3600
    }
]
```

On GCS, list the same headers under `responseHeader`.

## Operators

### import_multimodal

Choose a folder with the file browser and the operator reports what it found
(eg `Detected MCAP: 42 files, 6.1 GB`). Subfolders are always searched. Then
pick a new dataset name, or add to the dataset you have open if it holds the
same format.

To import only some files, enter a **File pattern**, relative to the chosen
folder:

| Pattern              | Imports                                      |
| -------------------- | -------------------------------------------- |
| _(empty)_            | every `.mcap` file in the folder and below   |
| `**/chopping*.mcap`  | files starting with `chopping`, at any depth |
| `run1/*.mcap`        | files directly inside `run1/`                |

This runs as a background (delegated) operation by default, so large folders
don't tie up the App.

A dataset holds either MCAP samples or LeRobot episodes, never both.
Re-running an import is safe: MCAP files already in the dataset are skipped,
and a LeRobot folder that was already imported is not added again.

### upload_multimodal

Drag in one `.mcap` file (up to `FIFTYONE_MULTIMODAL_IO_MAX_UPLOAD_MB`). The
file is saved to `<root>/users/<you>/<dataset>/` and imported. Uploads go
through the browser, so use the CLI for anything large.

## Upload script

`upload.py` needs the FiftyOne Enterprise SDK and a connection to your
deployment:

```shell
export FIFTYONE_API_URI=https://your-deployment.fiftyone.ai
export FIFTYONE_API_KEY=...   # your own API key

# Upload a local folder to <root>/users/<you>/kitchen-runs and import it
python upload.py ./recordings --dataset kitchen-runs \
    --root gs://acme/fiftyone

# Import data that is already in a bucket, without uploading
python upload.py s3://acme/fiftyone/shared/pick-place --dataset pick-place

# Only upload/import files matching a pattern (same rules as the App)
python upload.py ./recordings --dataset kitchen-runs \
    --root gs://acme/fiftyone --pattern "**/chopping*.mcap"

# Show the plan without changing anything
python upload.py ./recordings --dataset kitchen-runs --dry-run
```

Example output:

```
Source:      /data/recordings
Detected:    MCAP: 1 file, 15.4 MB
Upload to:   gs://acme/fiftyone/users/jane/kitchen-runs
Dataset:     kitchen-runs (will be created)
Uploaded 1 file(s), 15.4 MB; skipped 0 already uploaded

Added 1 sample(s) to 'kitchen-runs'
Dataset now has 1 sample(s)
```

Re-running the same command resumes an interrupted upload: files already in
the bucket with the same size are skipped.

Run `python upload.py --help` for all options.

## Tests

```shell
pytest tests
```
