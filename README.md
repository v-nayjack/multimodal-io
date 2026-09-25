## Multimodal I/O Plugin

This plugin brings robotics recordings into FiftyOne Enterprise from any cloud
bucket your deployment can reach (GCS, S3, Azure, MinIO). It supports two
formats:

-   **MCAP**: every `.mcap` file in a folder (including subfolders) becomes
    one sample
-   **LeRobot v3**: a folder containing `meta/info.json` is imported as one
    sample per episode

| Use case                                        | Tool                                          |
| ----------------------------------------------- | --------------------------------------------- |
| Browse a bucket and import a folder in place    | `import_multimodal` operator (App)            |
| Upload MCAP files of any size from your laptop  | **Upload MCAP files** panel (App)             |
| Upload whole folders or script it               | [`upload.py`](upload.py) script (CLI or SDK)  |

All paths share the same format detection and folder rules
([`core.py`](core.py)), so data lands in the same place however it arrives,
and a file that was already uploaded or imported is never added twice.

Requires FiftyOne Enterprise `>=2.25.0` with multimodal support enabled.

## Installation

An admin installs the plugin from the App (Settings > Plugins), or with the
management SDK:

```py
import fiftyone.management as fom

fom.upload_plugin("/path/to/multimodal-io", overwrite=True)
```

After installing or updating the plugin, **reload the App page** once. The
App loads plugin code when the page opens, so an already-open tab keeps
running the previous version.

To use it locally:

```shell
fiftyone plugins download https://github.com/v-nayjack/multimodal-io
```

## Configuration

Settings are read from plugin secrets (Settings > Secrets) or environment
variables of the same name. None of them hold credentials.

| Setting                                | Purpose                                                                                     | Default                              |
| -------------------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------ |
| `FIFTYONE_MULTIMODAL_IO_ROOT`          | One or more comma-separated bucket folders that imports are limited to and uploads can go to, eg `s3://acme/fiftyone, gs://acme-eu/fiftyone`. Users pick one in the upload panel; the first is the default | unset: browse anything, no uploads |
| `FIFTYONE_MULTIMODAL_IO_PATH_TEMPLATE` | Where uploads land, using `{root}`, `{username}`, `{dataset}`                              | `{root}/users/{username}/{dataset}`  |

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
curated data stays separate. Every sample added by the plugin or the script
also gets an indexed `uploaded_by` field with the username of whoever uploaded
or imported it, so you can filter by person in the App sidebar. If your team uses a different convention, change
`FIFTYONE_MULTIMODAL_IO_PATH_TEMPLATE`.

### Credentials

The plugin never asks for, stores, or logs bucket keys. In the App, all bucket
access happens on the server, as the deployment, so **users need no bucket
permissions of their own**, only a FiftyOne role that can run the plugin.

The server can get bucket access either way:

-   **Stored credentials**: added by an admin in Settings > Cloud storage
    ([docs](https://docs.voxel51.com/enterprise/cloud_media.html))
-   **Container credentials**: provided to the FiftyOne containers by the
    platform, eg an IAM role for the pods (IRSA), Workload Identity, or
    mounted credential files. See [Deployment setup](#deployment-setup)

The **upload script** is different: it runs on the user's machine. It works
when the deployment has *stored* credentials (the SDK fetches them with the
user's API key) or when the user has their own bucket credentials. On
deployments that only use container credentials, users should upload with the
**Upload MCAP files** panel instead.

### Bucket CORS

The browser talks to the bucket directly in two ways, and both need the
bucket's CORS policy to allow your deployment's origin:

| Need                           | Methods     | Exposed headers                                               |
| ------------------------------ | ----------- | ------------------------------------------------------------- |
| View recordings (range reads)  | `GET, HEAD` | `Content-Range`, `Content-Length`, `Accept-Ranges`, `Content-Type` |
| Upload panel                   | `PUT`       | `ETag` (S3/MinIO multipart), `Range` (GCS resumable)          |

Missing exposed headers fail quietly: imports succeed, but samples fail to open
with `Expected Content-Range header for byte-range response`, or uploads stop
with a message about `ETag`.

S3 example:

```json
[
    {
        "AllowedOrigins": ["https://your-deployment.fiftyone.ai"],
        "AllowedMethods": ["GET", "HEAD", "PUT"],
        "AllowedHeaders": ["*"],
        "ExposeHeaders": [
            "Content-Range",
            "Content-Length",
            "Accept-Ranges",
            "Content-Type",
            "ETag"
        ],
        "MaxAgeSeconds": 3600
    }
]
```

GCS example (`gcloud storage buckets update gs://BUCKET --cors-file=cors.json`):

```json
[
    {
        "origin": ["https://your-deployment.fiftyone.ai"],
        "method": ["GET", "HEAD", "PUT"],
        "responseHeader": [
            "Content-Type",
            "Content-Range",
            "Content-Length",
            "Accept-Ranges",
            "Range"
        ],
        "maxAgeSeconds": 3600
    }
]
```

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

### Upload MCAP files panel

Inside any dataset, click the **Upload MCAP files** button above the sample
grid (also in the operator browser and the `+` tab menu). Then:

1.  **Upload to**: pick one of the locations your admin allowed
    (`FIFTYONE_MULTIMODAL_IO_ROOT`). The panel remembers your choice
2.  **Dataset**: the dataset to upload into. The panel shows whether it will
    be created or added to, and the exact folder files go to:
    `<location>/users/<you>/<dataset>/`
3.  Drag in `.mcap` files or click **Choose files**. There is no size limit
4.  Click **Start upload**. Files upload one at a time; each shows its
    progress, speed, and time left, then is imported as soon as it finishes

How it works: the plugin creates short-lived signed upload links for your
folder only, and the browser sends the bytes straight to the bucket, never
through the FiftyOne server. S3/MinIO files go up in 64 MiB+ parts, four at
a time; GCS uses a resumable upload session; Azure uses a single upload (up to
5000 MiB). If a signed link expires mid-upload (common with temporary,
role-based server credentials), the panel fetches fresh links and continues.

-   **Keep the browser tab open** until uploads finish (the page warns
    before closing). Closing the panel or opening another dataset is fine:
    uploads keep going, and reopening the panel shows their progress
-   **Queue**: files upload one at a time; files added during an upload join
    the queue, and waiting files can be removed
-   **Cancel** stops the file that is uploading and discards what was sent;
    **Cancel all** also drops the waiting files. Files that already finished
    stay imported. A file that is importing can't be cancelled
-   **Resume**: if the connection drops or the tab reloads, add the same file
    again and click Start: only the missing parts are sent
-   A file that is already fully uploaded is not sent again
-   Leaving the dataset blank isn't allowed; a name that doesn't exist yet
    creates a new dataset. You can upload into any dataset, not just the one
    you have open. Each file shows the folder it goes to
-   When no upload is running, the Dataset field follows the dataset you have
    open

## Deployment setup

### Server containers that need bucket access

If your deployment uses container credentials instead of stored ones, give
them to these containers:

| Container       | Why                                              |
| --------------- | ------------------------------------------------ |
| `fiftyone-app`  | Browsing folders, reading files                  |
| `teams-plugins` | Plugin operators (if you run dedicated plugins)  |
| `teams-do`      | Background (delegated) imports                   |
| `teams-api`     | Signed links for viewing media                   |

Docker Compose example with mounted credential files, in
`compose.override.yaml` (repeat for each container above):

```yaml
services:
  fiftyone-app:
    environment:
      AWS_SHARED_CREDENTIALS_FILE: /opt/creds/aws-credentials
      AWS_DEFAULT_REGION: us-east-1
      GOOGLE_APPLICATION_CREDENTIALS: /opt/creds/gcs.json
    volumes:
      - /path/to/creds/aws-credentials:/opt/creds/aws-credentials:ro
      - /path/to/creds/gcs.json:/opt/creds/gcs.json:ro
```

On Kubernetes, attach the IAM role (or equivalent) to the service accounts of
the same pods.

### Permissions for the deployment's role (S3)

Scope these to the upload location so nothing can be written anywhere else:

| Permission                     | Used for                                            |
| ------------------------------ | --------------------------------------------------- |
| `s3:ListBucket` (prefix only)  | Browsing, finding MCAP/LeRobot files                |
| `s3:GetObject`                 | Importing, viewing recordings, checking uploads     |
| `s3:PutObject`                 | Uploads (incl. multipart steps) and `_import.json`  |
| `s3:ListMultipartUploadParts`  | Resuming an interrupted upload                      |
| `s3:AbortMultipartUpload`      | Cancel and Cancel all                               |
| `kms:GenerateDataKey`, `kms:Decrypt` | Only if the bucket uses KMS encryption       |

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": "arn:aws:s3:::UPLOAD_BUCKET",
            "Condition": {"StringLike": {"s3:prefix": ["UPLOAD_PREFIX/*"]}}
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:ListMultipartUploadParts",
                "s3:AbortMultipartUpload"
            ],
            "Resource": "arn:aws:s3:::UPLOAD_BUCKET/UPLOAD_PREFIX/*"
        }
    ]
}
```

Then set `FIFTYONE_MULTIMODAL_IO_ROOT` to `s3://UPLOAD_BUCKET/UPLOAD_PREFIX`.

### Bucket CORS with Terraform (S3)

```hcl
resource "aws_s3_bucket_cors_configuration" "fiftyone_uploads" {
  bucket = "UPLOAD_BUCKET"
  cors_rule {
    allowed_origins = ["https://your-deployment.fiftyone.ai"]
    allowed_methods = ["GET", "HEAD", "PUT"]
    allowed_headers = ["*"]
    expose_headers  = ["Content-Range", "Content-Length", "Accept-Ranges", "Content-Type", "ETag"]
    max_age_seconds = 3600
  }
}
```

A bucket has a single CORS configuration, so if one already exists, **add
these to it** instead of creating a second one, which would replace the rules
that let recordings render.

## Upload script

`upload.py` needs the FiftyOne Enterprise SDK, a connection to your
deployment, and bucket access from your machine (see
[Credentials](#credentials)):

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

## Development

The upload panel is a JS plugin component in `src/`, built into
`dist/index.umd.js` (committed, so installs need no build step):

```shell
yarn install
FIFTYONE_DIR=/path/to/fiftyone yarn build
```

`FIFTYONE_DIR` must point at a FiftyOne source checkout; the build uses it to
resolve `@fiftyone/*` packages.

Tests:

```shell
pytest tests
```
