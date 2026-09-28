"""
Cloud clients for signed uploads, built with the official SDKs.

Signed upload targets need a raw S3, GCS, or Azure client. These are created
with the official SDKs (``boto3``, ``google-cloud-storage``,
``azure-storage-blob``) and their standard credential lookup, which covers
environment variables, mounted credential files, and platform identities such
as IAM roles for pods, instance profiles, Workload Identity, and managed
identities. These are public, stable APIs, so FiftyOne upgrades don't affect
them.

Only when the server has no such credentials (eg a deployment that stores its
bucket credentials in Settings > Cloud storage, or MinIO and aliased paths)
does this fall back to the client FiftyOne builds internally.

| Copyright 2026, Vinay Jakkali
| Licensed under the Apache License, Version 2.0
|
"""

from datetime import datetime, timedelta, timezone
import functools
import os
import re

import fiftyone.core.storage as fos


_AZURE_URL = re.compile(
    r"^https://([a-z0-9]+)\.blob\.core\.windows\.net/([^/]+)/(.+)$"
)
_AZURE_IDENTITY_ENV = (
    "AZURE_CLIENT_ID",
    "AZURE_FEDERATED_TOKEN_FILE",
    "IDENTITY_ENDPOINT",
    "MSI_ENDPOINT",
)


class UploadClientError(Exception):
    """Raised when no usable client can be created for a path."""


def s3(path):
    """Returns ``(client, bucket, key)`` for an S3 or MinIO path.

    Args:
        path: an object path like ``s3://bucket/key``

    Returns:
        a ``(boto3 client, bucket, key)`` tuple
    """
    if path.startswith("s3://"):
        bucket, key = _split(path[len("s3://") :])
        client = _public_s3(bucket)
        if client is not None:
            return client, bucket, key

    return _fallback(path)


def gcs(path):
    """Returns ``(client, bucket, key)`` for a GCS path.

    Args:
        path: an object path like ``gs://bucket/key``

    Returns:
        a ``(google.cloud.storage.Client, bucket, key)`` tuple
    """
    if path.startswith("gs://"):
        bucket, key = _split(path[len("gs://") :])
        client = _public_gcs()
        if client is not None:
            return client, bucket, key

    return _fallback(path)


def azure_blob(path, hours):
    """Returns ``(blob_client, sas_url)`` for an Azure blob path.

    ``sas_url`` is a URL for the blob with a SAS token that allows reading and
    writing it for ``hours`` hours.

    Args:
        path: a blob path like
            ``https://account.blob.core.windows.net/container/blob``
        hours: how long the SAS token is valid

    Returns:
        a ``(BlobClient, sas_url)`` tuple, or ``None`` if the server has no
        Azure credentials of its own for this storage account
    """
    match = _AZURE_URL.match(path)
    if match is None:
        return None

    account, container, blob = match.groups()
    service, account_key = _public_azure(account)
    if service is None:
        return None

    from azure.storage.blob import BlobSasPermissions, generate_blob_sas

    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=hours)
    kwargs = {}
    if account_key:
        kwargs["account_key"] = account_key
    else:
        kwargs["user_delegation_key"] = service.get_user_delegation_key(
            now - timedelta(minutes=5), expiry
        )

    sas = generate_blob_sas(
        account_name=account,
        container_name=container,
        blob_name=blob,
        permission=BlobSasPermissions(read=True, write=True, create=True),
        start=now - timedelta(minutes=5),
        expiry=expiry,
        **kwargs,
    )
    blob_client = service.get_blob_client(container, blob)
    return blob_client, "%s?%s" % (blob_client.url, sas)


@functools.lru_cache(maxsize=32)
def _public_s3(bucket):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        return None

    session = boto3.session.Session()
    if session.get_credentials() is None:
        return None

    config = Config(
        signature_version="s3v4", s3={"addressing_style": "virtual"}
    )
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or session.region_name
    )
    if not region:
        # Signed URLs must use the bucket's own region
        try:
            resp = session.client("s3", config=config).get_bucket_location(
                Bucket=bucket
            )
            region = resp.get("LocationConstraint") or "us-east-1"
        except Exception:
            region = "us-east-1"

    return session.client("s3", region_name=region, config=config)


@functools.lru_cache(maxsize=1)
def _public_gcs():
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
        from google.cloud import storage
    except ImportError:
        return None

    try:
        credentials, project = google.auth.default()
    except DefaultCredentialsError:
        return None

    return storage.Client(credentials=credentials, project=project)


@functools.lru_cache(maxsize=32)
def _public_azure(account):
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        return None, None

    account_url = "https://%s.blob.core.windows.net" % account

    # Same credentials file FiftyOne's own Azure client reads
    creds = _azure_credentials_file()
    if creds.get("conn_str"):
        service = BlobServiceClient.from_connection_string(creds["conn_str"])
        if service.account_name == account:
            return service, service.credential.account_key

    if creds.get("account_key") and creds.get("account_name") == account:
        service = BlobServiceClient(
            account_url,
            credential={
                "account_name": account,
                "account_key": creds["account_key"],
            },
        )
        return service, creds["account_key"]

    if creds.get("client_id") and creds.get("client_secret"):
        try:
            from azure.identity import ClientSecretCredential
        except ImportError:
            return None, None

        credential = ClientSecretCredential(
            creds["tenant_id"], creds["client_id"], creds["client_secret"]
        )
        return BlobServiceClient(account_url, credential), None

    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn_str:
        service = BlobServiceClient.from_connection_string(conn_str)
        if service.account_name == account:
            return service, service.credential.account_key

    key = os.environ.get("AZURE_STORAGE_KEY")
    if key and os.environ.get("AZURE_STORAGE_ACCOUNT") == account:
        service = BlobServiceClient(
            account_url,
            credential={"account_name": account, "account_key": key},
        )
        return service, key

    # Service principal, workload identity, or managed identity. Only tried
    # when configured, since probing for a managed identity can be slow
    if any(os.environ.get(k) for k in _AZURE_IDENTITY_ENV):
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError:
            return None, None

        return BlobServiceClient(account_url, DefaultAzureCredential()), None

    return None, None


def _azure_credentials_file():
    """Reads ``AZURE_CREDENTIALS_FILE``, if set, in FiftyOne's ``.ini``
    format (``[default]`` section with ``conn_str``, or ``account_name`` and
    ``account_key``, or ``client_id``, ``client_secret``, and
    ``tenant_id``)."""
    path = os.environ.get("AZURE_CREDENTIALS_FILE")
    if not path or not os.path.isfile(path):
        return {}

    import configparser

    config = configparser.ConfigParser()
    config.read(path)
    profile = os.environ.get("AZURE_PROFILE") or "default"
    for section in (profile, "profile " + profile):
        if section in config:
            return dict(config[section])

    return {}


def _fallback(path):
    # Deployments that store bucket credentials in the App (and MinIO or
    # aliased paths) are only reachable through the client FiftyOne builds
    # internally. This is the one place that relies on FiftyOne internals
    try:
        storage_client = fos.get_client(path=path)
        bucket, key = storage_client._parse_path(path)
        return storage_client._client, bucket, key
    except AttributeError as e:
        raise UploadClientError(
            "Can't create an upload client for '%s'. Give the FiftyOne "
            "containers their own bucket credentials (see the plugin README), "
            "or update the plugin for this FiftyOne version" % path
        ) from e


def _split(bucket_and_key):
    parts = bucket_and_key.split("/", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""
