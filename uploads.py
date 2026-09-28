"""
Direct browser-to-bucket uploads for large files.

The server never handles file bytes. Instead it creates short-lived signed
upload targets for one exact object path, the browser sends the bytes straight
to the bucket, and the server finalizes the upload:

-   S3 and MinIO: multipart upload with a presigned URL per part (any size,
    resumable by part)
-   GCS: resumable upload session (any size, resumable by byte offset)
-   Azure: block blob upload with a SAS URL (any size, resumable by block)

Clients come from :mod:`clients`, which uses the official cloud SDKs.

| Copyright 2026, Vinay Jakkali
| Licensed under the Apache License, Version 2.0
|
"""

import base64
import math

import fiftyone.core.storage as fos

try:
    from . import clients
except ImportError:
    import clients


S3_MULTIPART = "s3_multipart"
GCS_RESUMABLE = "gcs_resumable"
AZURE_BLOCKS = "azure_blocks"
SINGLE_PUT = "single_put"

MIB = 1024 * 1024
MIN_PART_SIZE = 64 * MIB
MAX_PARTS = 1000
GCS_CHUNK_SIZE = 64 * MIB  # must be a multiple of 256 KiB
SINGLE_PUT_MAX_BYTES = 5000 * MIB
SIGNED_URL_HOURS = 12


def start_upload(path, size, origin=None):
    """Starts an upload of ``size`` bytes to ``path``.

    Args:
        path: the destination object path, eg ``s3://bucket/key.mcap``
        size: the file size, in bytes
        origin (None): the browser origin that will send the bytes, eg
            ``https://acme.fiftyone.ai``. Required for GCS

    Returns:
        an upload plan dict whose ``mode`` tells the browser how to send
        the bytes
    """
    fs = fos.get_file_system(path)

    if fs in (fos.FileSystem.S3, fos.FileSystem.MINIO):
        return _start_s3(path, size)

    if fs == fos.FileSystem.GCS:
        return _start_gcs(path, size, origin)

    if fs == fos.FileSystem.AZURE:
        return _start_azure(path, size)

    raise ValueError(
        "Browser uploads are not supported for '%s'. Use the upload script "
        "instead" % path
    )


def resume_upload(path, size, upload_id=None, mode=S3_MULTIPART):
    """Returns what is needed to continue an interrupted upload.

    Also used to get fresh signed links when the old ones expire.

    Args:
        path: the destination object path
        size: the file size, in bytes
        upload_id (None): the S3 multipart upload ID from
            :func:`start_upload`
        mode (S3_MULTIPART): the plan ``mode`` from :func:`start_upload`

    Returns:
        a plan dict like :func:`start_upload`, plus ``done``, a list of
        ``{"number", "etag"}`` dicts for parts already uploaded
    """
    if mode == AZURE_BLOCKS:
        blob_client, url = _azure(path)
        _, uncommitted = blob_client.get_block_list("uncommitted")
        done = [
            {"number": n, "etag": None}
            for n in (block_number(b.id) for b in uncommitted)
            if n is not None
        ]
        plan = _azure_plan(url, size)
        plan["done"] = done
        return plan

    client, bucket, key = clients.s3(path)
    done = []
    kwargs = dict(Bucket=bucket, Key=key, UploadId=upload_id)
    while True:
        resp = client.list_parts(**kwargs)
        done.extend(
            {"number": p["PartNumber"], "etag": p["ETag"]}
            for p in resp.get("Parts", [])
        )
        if not resp.get("IsTruncated"):
            break

        kwargs["PartNumberMarker"] = resp["NextPartNumberMarker"]

    plan = _s3_plan(client, bucket, key, upload_id, size)
    plan["done"] = done
    return plan


def complete_upload(path, mode, upload_id=None, parts=None, size=None):
    """Finalizes an upload once the browser has sent all bytes.

    Args:
        path: the destination object path
        mode: the plan ``mode`` from :func:`start_upload`
        upload_id (None): the S3 multipart upload ID
        parts (None): for S3, a list of ``{"number", "etag"}`` dicts
        size (None): the file size, in bytes. Required for Azure

    Returns:
        the size of the uploaded object, in bytes
    """
    if mode == S3_MULTIPART:
        client, bucket, key = clients.s3(path)
        parts = sorted(parts or [], key=lambda p: int(p["number"]))
        if not parts:
            raise ValueError("No uploaded parts to complete")

        client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": int(p["number"]), "ETag": p["etag"]}
                    for p in parts
                ]
            },
        )

    elif mode == AZURE_BLOCKS:
        from azure.storage.blob import BlobBlock

        blob_client, _ = _azure(path)
        num_blocks = _num_parts(size)
        blob_client.commit_block_list(
            [
                BlobBlock(block_id=block_name(n))
                for n in range(1, num_blocks + 1)
            ]
        )

    if not fos.isfile(path):
        raise ValueError("Upload did not finish: '%s' not found" % path)

    return object_size(path)


def object_size(path):
    """Returns the size of an uploaded object, in bytes.

    On Azure, ``fiftyone.core.storage.get_file_size()`` fails (FiftyOne asks
    its Azure client for a ``HEAD`` signed URL, which it doesn't support), so
    Azure sizes come from the blob's properties via the Azure SDK.

    Args:
        path: the object path

    Returns:
        the size in bytes
    """
    if fos.get_file_system(path) == fos.FileSystem.AZURE:
        target = clients.azure_blob(path, SIGNED_URL_HOURS)
        if target is not None:
            return target[0].get_blob_properties().size

    return fos.get_file_size(path)


def abort_upload(path, mode, upload_id=None):
    """Cancels an in-progress upload and discards any uploaded parts.

    Azure discards uncommitted blocks on its own after 7 days, and GCS
    discards unfinished resumable sessions after a week.

    Args:
        path: the destination object path
        mode: the plan ``mode`` from :func:`start_upload`
        upload_id (None): the S3 multipart upload ID
    """
    if mode == S3_MULTIPART and upload_id:
        client, bucket, key = clients.s3(path)
        client.abort_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id
        )


def part_size_for(size):
    """Returns the part (or block) size for a file of ``size`` bytes.

    Parts are at least 64 MiB, and large enough that no file needs more than
    :const:`MAX_PARTS` parts.

    Args:
        size: the file size, in bytes

    Returns:
        the part size, in bytes
    """
    part_size = max(MIN_PART_SIZE, math.ceil(size / MAX_PARTS))
    return math.ceil(part_size / MIB) * MIB


def block_name(number):
    """Returns the Azure block name for block ``number`` (starting at 1).

    This is the plain form the Azure SDK expects; the SDK base64-encodes it
    when talking to Azure. All names in a blob have the same length, as Azure
    requires.

    Args:
        number: the block number

    Returns:
        the block name
    """
    return "%06d" % number


def block_id(number):
    """Returns the base64 block ID for block ``number``, as sent to Azure's
    REST API. The browser computes the same IDs.

    Args:
        number: the block number

    Returns:
        the block ID
    """
    return base64.b64encode(block_name(number).encode()).decode()


def block_number(name):
    """Inverse of :func:`block_name` (or :func:`block_id`), or ``None`` for
    blocks this plugin didn't create."""
    if name.isdigit():
        return int(name)

    try:
        decoded = base64.b64decode(name, validate=True).decode()
    except Exception:
        return None

    return int(decoded) if decoded.isdigit() else None


def _num_parts(size):
    return max(1, math.ceil(int(size) / part_size_for(int(size))))


def _start_s3(path, size):
    client, bucket, key = clients.s3(path)
    resp = client.create_multipart_upload(
        Bucket=bucket, Key=key, ContentType="application/octet-stream"
    )
    return _s3_plan(client, bucket, key, resp["UploadId"], size)


def _s3_plan(client, bucket, key, upload_id, size):
    part_size = part_size_for(size)
    parts = []
    for number in range(1, _num_parts(size) + 1):
        url = client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": number,
            },
            ExpiresIn=SIGNED_URL_HOURS * 3600,
        )
        parts.append({"number": number, "url": url})

    return {
        "mode": S3_MULTIPART,
        "upload_id": upload_id,
        "part_size": part_size,
        "parts": parts,
    }


def _start_gcs(path, size, origin):
    if not origin:
        raise ValueError("The browser origin is required for GCS uploads")

    client, bucket, key = clients.gcs(path)
    session_url = (
        client.bucket(bucket)
        .blob(key)
        .create_resumable_upload_session(
            content_type="application/octet-stream", size=size, origin=origin
        )
    )
    return {
        "mode": GCS_RESUMABLE,
        "session_url": session_url,
        "chunk_size": GCS_CHUNK_SIZE,
    }


def _start_azure(path, size):
    target = clients.azure_blob(path, SIGNED_URL_HOURS)
    if target is not None:
        return _azure_plan(target[1], size)

    # No Azure credentials in the containers: fall back to one signed PUT
    # through FiftyOne's public API, which has a size limit
    if size > SINGLE_PUT_MAX_BYTES:
        raise ValueError(
            "Files over %d MiB need Azure credentials in the FiftyOne "
            "containers for browser uploads (see the plugin README)"
            % (SINGLE_PUT_MAX_BYTES // MIB)
        )

    url = fos.get_url(path, method="PUT", hours=SIGNED_URL_HOURS)
    return {
        "mode": SINGLE_PUT,
        "url": url,
        "headers": {"x-ms-blob-type": "BlockBlob"},
    }


def _azure_plan(sas_url, size):
    return {
        "mode": AZURE_BLOCKS,
        "url": sas_url,
        "part_size": part_size_for(size),
        "num_parts": _num_parts(size),
    }


def _azure(path):
    target = clients.azure_blob(path, SIGNED_URL_HOURS)
    if target is None:
        raise clients.UploadClientError(
            "No Azure credentials for '%s' in the FiftyOne containers" % path
        )

    return target
