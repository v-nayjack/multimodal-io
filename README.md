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
and a file that was already uploaded or imported is never added twice
(unless you choose to overwrite it).

Requires FiftyOne Enterprise `>=2.25.0` with multimodal support enabled.

## Demo

**Import MCAP recordings from a bucket**

https://github.com/user-attachments/assets/3ae2098a-ad3e-4ff6-8901-1674f4589622

**Import a LeRobot dataset**

https://github.com/user-attachments/assets/81bf2ccf-93cc-4e40-899e-842dc9279603

**Upload MCAP files from your laptop, to S3, GCS, or Azure**

https://github.com/user-attachments/assets/7b6d5ee5-c715-46eb-a2b5-294f07f4ad82

The sample data is credited in [Demo data](#demo-data).

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
permissions of their own**, only a FiftyOne role that can create datasets and
run the plugin (Member or above), plus edit access to any existing dataset
they add to.

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

Azure: in the storage account, **Settings > Resource sharing (CORS) > Blob
service**, add allowed origin `https://your-deployment.fiftyone.ai`, methods
`GET, HEAD, PUT, OPTIONS`, allowed headers `*`, max age `3600`, and **list the
exposed headers explicitly**:
`Content-Range,Content-Length,Accept-Ranges,Content-Type,ETag`. Don't use `*`
for exposed headers: Azure expands it to a list without `Content-Range`, so
uploads work but recordings fail to open.

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
grid (also in the operator browser and the `+` tab menu).

In an **empty dataset** (for example one you just created), FiftyOne shows its
"No samples yet" page instead of the grid, so panels can't open there. Use
**browse operations** > **Upload MCAP files** instead: the same upload UI opens
in a dialog. Keep the dialog open until your uploads finish; the page then
reloads and shows the new samples (clicking **Done** early also reloads, and
the browser asks first if uploads are still running).

Then:

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
a time; GCS uses a resumable upload session; Azure uploads blocks of the same
size, four at a time. If a signed link expires mid-upload (common with temporary,
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
-   A file that is already in the bucket with the same name and size is not
    sent again
-   **Overwrite files that already exist**: check this to replace files
    with the same name, for example after re-processing recordings. The new
    file replaces the one in the bucket, and its sample is refreshed: its
    metadata (size, streams) is recomputed from the new file, any tags are
    added, and `uploaded_by` becomes you. The row then shows "replaced".
    It's off by default each time the page loads
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

The plugin uses the standard AWS and Google credential lookup, so any method
your platform supports works without changes. Prefer methods that don't put
long-lived keys on disk:

| Where FiftyOne runs            | Recommended                                              |
| ------------------------------ | -------------------------------------------------------- |
| Kubernetes on AWS (EKS)        | IAM role for the pods' service account (IRSA or EKS Pod Identity) |
| Docker Compose on AWS (EC2)    | IAM role attached to the VM (instance profile)           |
| Google Cloud (GKE or VM)       | Workload Identity or the VM's service account            |
| Anywhere, for quick testing    | Mounted credential files (below)                          |

Mounted credential files, for testing with Docker Compose. Add this to
`compose.override.yaml` for each container above. The file names follow the
usual conventions (AWS's `credentials` ini file and a Google service account
`key.json`), but any name works as long as the variables point to it:

```yaml
services:
  fiftyone-app:
    environment:
      AWS_SHARED_CREDENTIALS_FILE: /opt/creds/aws/credentials
      AWS_DEFAULT_REGION: us-east-1   # your bucket's region
      GOOGLE_APPLICATION_CREDENTIALS: /opt/creds/gcp/key.json
    volumes:
      - /path/to/aws/credentials:/opt/creds/aws/credentials:ro
      - /path/to/gcp/key.json:/opt/creds/gcp/key.json:ro
```

Keep these files readable by the containers (eg `chmod 644`) but inside a
private folder on the host (eg `chmod 700`), and never commit them.

### Permissions for the deployment's role (S3)

These go on the role the FiftyOne containers use, not on individual users.
Scope them to the upload location so nothing can be written anywhere else:

| Permission                           | Used for                                                  | Level |
| ------------------------------------ | --------------------------------------------------------- | ----- |
| `s3:ListBucket` (prefix only)        | Browsing folders, finding MCAP/LeRobot files              | Required |
| `s3:GetObject`                       | Importing, viewing recordings, checking finished uploads  | Required |
| `s3:PutObject`                       | Uploads (including multipart steps) and `_import.json`    | Required for uploads |
| `s3:ListMultipartUploadParts`        | Resuming interrupted uploads, and getting fresh upload links when they expire | Required for uploads with temporary (role-based) credentials; recommended otherwise |
| `s3:AbortMultipartUpload`            | Cancel and Cancel all discard the partial upload           | Recommended |
| `kms:GenerateDataKey`, `kms:Decrypt` | Reading and writing a KMS-encrypted bucket                | Required only if the bucket uses KMS |

Without `PutObject`, the plugin still imports and displays data that is
already in the bucket, but can't upload. Without `AbortMultipartUpload`,
cancelled uploads leave hidden partial data behind; a bucket lifecycle rule
that aborts incomplete multipart uploads after a few days cleans that up
either way.

If the role already reads this bucket (for example, recordings already render
in the App), it most likely has `ListBucket` and `GetObject`; the new parts are
usually `PutObject`, `ListMultipartUploadParts`, `AbortMultipartUpload`, and
the CORS change below.

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

### Permissions on GCS and Azure

-   **GCS**: the service account the containers use needs
    `storage.objects.list`, `storage.objects.get`, and
    `storage.objects.create` on the bucket (eg the **Storage Object Viewer**
    and **Storage Object Creator** roles)
-   **Azure**: give the containers either the storage account's connection
    string (`AZURE_STORAGE_CONNECTION_STRING`) or its name and key
    (`AZURE_STORAGE_ACCOUNT`, `AZURE_STORAGE_KEY`), or use a managed identity
    or service principal (`AZURE_CLIENT_ID` and related variables) with the
    **Storage Blob Data Contributor** role on the container. These are the
    same variables FiftyOne itself reads. Azure paths look like
    `https://ACCOUNT.blob.core.windows.net/CONTAINER/PREFIX`

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

## Staying working across FiftyOne upgrades

Signed upload links are created with the official cloud SDKs (`boto3`,
`google-cloud-storage`, `azure-storage-blob`), which ship with FiftyOne
Enterprise, using the containers' own credentials. These are public, stable
APIs, so FiftyOne upgrades don't affect uploads on deployments that give the
containers bucket access.

Only deployments that store bucket credentials in Settings > Cloud storage
(and MinIO or aliased paths) fall back to FiftyOne's internal storage client,
which a future FiftyOne release could change; the error message then says so.
Azure without container credentials falls back to a single signed upload
through FiftyOne's public API, limited to 5000 MiB per file.

After upgrading FiftyOne, or after changing bucket permissions, run the
upload check inside the plugin container. It uploads a small test file to
each location the same way the panel does, checks the permission used for
resuming, verifies the file, and deletes it:

```shell
docker compose exec teams-plugins \
    python /opt/plugins/@v-nayjack/multimodal-io/tools/check_uploads.py \
    s3://my-bucket/fiftyone gs://my-bucket/fiftyone
```

```
PASS  s3://my-bucket/fiftyone  (s3_multipart upload, container credentials)
PASS  gs://my-bucket/fiftyone  (gcs_resumable upload, container credentials)
```

## Upload script

`upload.py` uploads from the command line instead of the App. It runs on
your machine, so it needs bucket access from there:

| Your deployment                                  | Use                           |
| ------------------------------------------------ | ----------------------------- |
| Stores bucket credentials in Settings > Cloud storage | Script or panel (the script fetches the stored credentials with your API key) |
| Only gives the FiftyOne containers bucket access | The **Upload MCAP files** panel, unless you have your own bucket credentials |
| Scripted or pipeline uploads with their own credentials | Script |

It needs the FiftyOne Enterprise SDK and a connection to your deployment:

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

To replace files that changed (for example after re-processing), add
`--overwrite`: every file is uploaded again, replacing the one in the bucket,
and the samples of replaced MCAP files are refreshed the same way as the
panel's Overwrite option. LeRobot files are re-uploaded, but the dataset is
not re-imported.

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

## Demo data

The demo recordings use public sample datasets. They are not included in
this repo and are shown for demonstration only.

-   **Kitchen MCAP recordings**:
    [MCAP-Housing](https://huggingface.co/datasets/cortexdatalabs/MCAP-Housing)
    by [Cortex Data Labs](https://huggingface.co/cortexdatalabs), licensed
    [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/). The
    upload clips are shortened excerpts of these recordings. Non-commercial
    use only; contact Cortex Data Labs for commercial licensing
-   **LeRobot dataset**:
    [svla_so101_pickplace](https://huggingface.co/datasets/lerobot/svla_so101_pickplace)
    by [Hugging Face LeRobot](https://huggingface.co/lerobot), licensed
    [Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0)

The plugin itself is licensed under Apache 2.0 and does not include or
redistribute any of this data.

## Contributing

Pull requests go to the `develop` branch; `main` holds tested releases. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE). Copyright 2026 Vinay Jakkali.
