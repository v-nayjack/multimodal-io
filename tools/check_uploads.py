"""
Checks that browser uploads work from inside a FiftyOne container.

Run it after a FiftyOne upgrade, or after granting bucket permissions, to
confirm each upload location works end to end: it uploads a small test file
the same way the Upload MCAP files panel does (signed links, then completing
the upload), checks the permission used to resume uploads, verifies the
result, and deletes the test file.

Usage (from the host)::

    docker compose exec teams-plugins \\
        python /opt/plugins/@v-nayjack/multimodal-io/tools/check_uploads.py

With no arguments, it checks every location in
``FIFTYONE_MULTIMODAL_IO_ROOT``. You can also pass locations explicitly::

    ... check_uploads.py s3://my-bucket/fiftyone gs://my-bucket/fiftyone

| Copyright 2026, Vinay Jakkali
| Licensed under the Apache License, Version 2.0
|
"""

import os
import sys
import uuid

import requests

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)

import fiftyone.core.storage as fos  # noqa: E402

import clients  # noqa: E402
import uploads  # noqa: E402


TEST_BYTES = 1024 * 1024
ORIGIN = "https://upload-check.invalid"


def check(root):
    """Uploads, verifies, and deletes a small test file under ``root``.

    Args:
        root: an upload location, eg ``s3://my-bucket/fiftyone``

    Returns:
        a one-line result message
    """
    path = fos.join(root, "_upload_check", "%s.bin" % uuid.uuid4().hex)
    data = os.urandom(TEST_BYTES)

    plan = uploads.start_upload(path, len(data), origin=ORIGIN)
    mode = plan["mode"]
    parts = None

    if mode == uploads.S3_MULTIPART:
        resp = requests.put(plan["parts"][0]["url"], data=data, timeout=120)
        resp.raise_for_status()
        # Resuming needs s3:ListMultipartUploadParts
        done = uploads.resume_upload(path, len(data), plan["upload_id"])[
            "done"
        ]
        if not done:
            raise RuntimeError("uploaded part not listed (resume would fail)")
        parts = [{"number": 1, "etag": resp.headers["ETag"]}]
    elif mode == uploads.AZURE_BLOCKS:
        url = "%s&comp=block&blockid=%s" % (plan["url"], uploads.block_id(1))
        requests.put(url, data=data, timeout=120).raise_for_status()
        done = uploads.resume_upload(path, len(data), mode=mode)["done"]
        if not done:
            raise RuntimeError("uploaded block not listed (resume would fail)")
    elif mode == uploads.GCS_RESUMABLE:
        headers = {
            "Content-Range": "bytes 0-%d/%d" % (len(data) - 1, len(data))
        }
        resp = requests.put(
            plan["session_url"], data=data, headers=headers, timeout=120
        )
        resp.raise_for_status()
    elif mode == uploads.SINGLE_PUT:
        resp = requests.put(
            plan["url"], data=data, headers=plan.get("headers"), timeout=120
        )
        resp.raise_for_status()

    size = uploads.complete_upload(
        path,
        mode,
        upload_id=plan.get("upload_id"),
        parts=parts,
        size=len(data),
    )
    if size != len(data):
        raise RuntimeError(
            "uploaded %d bytes but found %d" % (len(data), size)
        )

    fos.delete_file(path)
    return "%s upload, %s" % (mode, _credentials_source(root, mode))


def _credentials_source(root, mode):
    if mode == uploads.SINGLE_PUT:
        return "FiftyOne stored credentials (size-limited, add Azure credentials to the containers for large files)"

    if root.startswith("s3://") and clients._public_s3(root[5:].split("/")[0]):
        return "container credentials"

    if root.startswith("gs://") and clients._public_gcs():
        return "container credentials"

    if mode == uploads.AZURE_BLOCKS:
        return "container credentials"

    return "FiftyOne stored credentials (relies on FiftyOne internals)"


def main(argv):
    roots = argv or [
        r.strip().rstrip("/")
        for r in os.environ.get("FIFTYONE_MULTIMODAL_IO_ROOT", "").split(",")
        if r.strip()
    ]
    if not roots:
        print(
            "No locations to check. Pass them as arguments or set "
            "FIFTYONE_MULTIMODAL_IO_ROOT"
        )
        return 2

    failed = 0
    for root in roots:
        try:
            print("PASS  %s  (%s)" % (root, check(root)))
        except Exception as e:
            failed += 1
            print("FAIL  %s  %s: %s" % (root, type(e).__name__, e))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
