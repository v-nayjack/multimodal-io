"""
Direct browser-to-bucket uploads for large files.

The server never handles file bytes. Instead it creates short-lived signed
upload targets for one exact object path, the browser sends the bytes straight
to the bucket, and the server finalizes the upload:

-   S3 and MinIO: multipart upload with a presigned URL per part (any size,
    resumable by part)
-   GCS: resumable upload session (any size, resumable by byte offset)
-   Azure: a single signed ``PUT`` (up to 5000 MiB)

| Copyright 2026, Vinay Jakkali
| Licensed under the Apache License, Version 2.0
|
"""

import math

import fiftyone.core.storage as fos


S3_MULTIPART = "s3_multipart"
GCS_RESUMABLE = "gcs_resumable"
SINGLE_PUT = "single_put"

MIB = 1024 * 1024
MIN_PART_SIZE = 64 * MIB
MAX_PARTS = 1000
GCS_CHUNK_SIZE = 64 * MIB  # must be a multiple of 256 KiB
AZURE_MAX_BYTES = 5000 * MIB
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
        return _start_single(path, size)

    raise ValueError(
        "Browser uploads are not supported for '%s'. Use the upload script "
        "instead" % path
    )


def resume_upload(path, size, upload_id):
    """Returns what is needed to resume an S3 multipart upload.

    Args:
        path: the destination object path
        size: the file size, in bytes
        upload_id: the multipart upload ID from :func:`start_upload`

    Returns:
        a plan dict like :func:`start_upload`, plus ``done``, a list of
        ``{"number", "etag"}`` dicts for parts already uploaded
    """
    client, bucket, key = _s3(path)
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


def complete_upload(path, mode, upload_id=None, parts=None):
    """Finalizes an upload once the browser has sent all bytes.

    Args:
        path: the destination object path
        mode: the plan ``mode`` from :func:`start_upload`
        upload_id (None): the S3 multipart upload ID
        parts (None): for S3, a list of ``{"number", "etag"}`` dicts

    Returns:
        the size of the uploaded object, in bytes
    """
    if mode == S3_MULTIPART:
        client, bucket, key = _s3(path)
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

    if not fos.isfile(path):
        raise ValueError("Upload did not finish: '%s' not found" % path)

    return fos.get_file_size(path)


def abort_upload(path, mode, upload_id=None):
    """Cancels an in-progress upload and discards any uploaded parts.

    Args:
        path: the destination object path
        mode: the plan ``mode`` from :func:`start_upload`
        upload_id (None): the S3 multipart upload ID
    """
    if mode == S3_MULTIPART and upload_id:
        client, bucket, key = _s3(path)
        client.abort_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id
        )


def part_size_for(size):
    """Returns the S3 part size for a file of ``size`` bytes.

    Parts are at least 64 MiB, and large enough that no file needs more than
    :const:`MAX_PARTS` parts.

    Args:
        size: the file size, in bytes

    Returns:
        the part size, in bytes
    """
    part_size = max(MIN_PART_SIZE, math.ceil(size / MAX_PARTS))
    return math.ceil(part_size / MIB) * MIB


def _start_s3(path, size):
    client, bucket, key = _s3(path)
    resp = client.create_multipart_upload(
        Bucket=bucket, Key=key, ContentType="application/octet-stream"
    )
    return _s3_plan(client, bucket, key, resp["UploadId"], size)


def _s3_plan(client, bucket, key, upload_id, size):
    part_size = part_size_for(size)
    num_parts = max(1, math.ceil(size / part_size))
    parts = []
    for number in range(1, num_parts + 1):
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

    storage_client = fos.get_client(path=path)
    bucket, key = storage_client._parse_path(path)
    blob = storage_client._client.bucket(bucket).blob(key)
    session_url = blob.create_resumable_upload_session(
        content_type="application/octet-stream", size=size, origin=origin
    )
    return {
        "mode": GCS_RESUMABLE,
        "session_url": session_url,
        "chunk_size": GCS_CHUNK_SIZE,
    }


def _start_single(path, size):
    if size > AZURE_MAX_BYTES:
        raise ValueError(
            "Files over %d MiB can't be uploaded to this storage from the "
            "browser yet. Use the upload script instead"
            % (AZURE_MAX_BYTES // MIB)
        )

    url = fos.get_url(path, method="PUT", hours=SIGNED_URL_HOURS)
    return {
        "mode": SINGLE_PUT,
        "url": url,
        "headers": {"x-ms-blob-type": "BlockBlob"},
    }


def _s3(path):
    storage_client = fos.get_client(path=path)
    bucket, key = storage_client._parse_path(path)
    return storage_client._client, bucket, key
