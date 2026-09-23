"""
Storage-agnostic helpers shared by the multimodal I/O plugin and its CLI.

Everything here goes through :mod:`fiftyone.core.storage`, which picks the
backend (local, S3, GCS, Azure, MinIO) from the path, so the same code runs
against any bucket the caller has credentials for.

| Copyright 2017-2026, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import re

import fiftyone as fo
import fiftyone.core.storage as fos


MCAP = "mcap"
LEROBOT = "lerobot"
FORMATS = (MCAP, LEROBOT)

MCAP_EXTS = (".mcap",)
LEROBOT_INFO_PATH = "meta/info.json"

DEFAULT_PATH_TEMPLATE = "{root}/users/{username}/{dataset}"
IMPORT_RECORD = "_import.json"
INFO_KEY = "multimodal_io"

_UNSAFE_CHARS = re.compile(r"[^a-z0-9._-]+")


@dataclass
class ScanResult:
    """What :func:`scan` found at a location.

    Args:
        path: the scanned location
        format: :const:`MCAP`, :const:`LEROBOT`, or ``None`` if nothing
            importable was found
        files: the MCAP filepaths to import (empty for LeRobot)
        num_files: the number of importable files
        total_bytes: the total size of the importable files, if known
        info: format-specific details (eg LeRobot ``meta/info.json``)
    """

    path: str
    format: str = None
    files: list = field(default_factory=list)
    num_files: int = 0
    total_bytes: int = 0
    info: dict = field(default_factory=dict)

    def describe(self):
        """Returns a one-line human-readable summary."""
        if self.format == LEROBOT:
            return "LeRobot v%s: %s episodes, %s frames, %s" % (
                str(self.info.get("codebase_version", "?")).lstrip("v"),
                self.info.get("total_episodes", "?"),
                self.info.get("total_frames", "?"),
                self.info.get("robot_type") or "unknown robot",
            )

        if self.format == MCAP:
            return "MCAP: %d file%s, %s" % (
                self.num_files,
                "" if self.num_files == 1 else "s",
                format_bytes(self.total_bytes),
            )

        return "No MCAP files or LeRobot dataset found"


def scan(path):
    """Detects whether ``path`` holds a LeRobot dataset or MCAP files.

    A directory containing ``meta/info.json`` is treated as a LeRobot v3
    dataset. Otherwise, all ``.mcap`` files at or below ``path`` are
    collected. ``path`` may also be a single ``.mcap`` file.

    Args:
        path: a local or remote file or directory

    Returns:
        a :class:`ScanResult`
    """
    path = fos.normalize_path(path).rstrip("/")

    if _is_mcap(path) and fos.isfile(path):
        size = _file_size(path)
        return ScanResult(
            path=path,
            format=MCAP,
            files=[path],
            num_files=1,
            total_bytes=size,
        )

    info_path = fos.join(path, LEROBOT_INFO_PATH)
    if fos.isfile(info_path):
        info = fos.read_json(info_path)
        return ScanResult(
            path=path,
            format=LEROBOT,
            num_files=1,
            info=info,
        )

    files, total = [], 0
    for entry in fos.list_files(path, recursive=True, return_metadata=True):
        relpath = entry["filepath"]
        if not _is_mcap(relpath) or _is_hidden(relpath):
            continue

        files.append(fos.join(path, relpath))
        total += entry.get("size") or 0

    files.sort()
    return ScanResult(
        path=path,
        format=MCAP if files else None,
        files=files,
        num_files=len(files),
        total_bytes=total,
    )


def import_scan(
    dataset, result, tags=None, compute_metadata=True, progress=None
):
    """Adds the contents of a :class:`ScanResult` to ``dataset``.

    MCAP files already in the dataset are skipped, so re-running an import
    on the same location only adds new files.

    Args:
        dataset: a :class:`fiftyone.core.dataset.Dataset`
        result: a :class:`ScanResult`
        tags (None): optional tag(s) to add to each new sample
        compute_metadata (True): whether to populate metadata for new MCAP
            samples
        progress (None): an optional progress callback, as accepted by
            :meth:`fiftyone.core.dataset.Dataset.add_samples`

    Returns:
        the list of new sample IDs
    """
    if result.format is None:
        raise ValueError("Nothing to import at '%s'" % result.path)

    check_compatible(dataset, result.format)

    if result.format == LEROBOT:
        ids = dataset.add_dir(
            dataset_dir=result.path,
            dataset_type=fo.types.LeRobotDataset,
            tags=tags,
            progress=progress,
        )
    else:
        existing = set(dataset.values("filepath")) if len(dataset) else set()
        new_files = [f for f in result.files if f not in existing]
        samples = [
            fo.Sample(filepath=f, tags=list(tags or [])) for f in new_files
        ]
        ids = (
            dataset.add_samples(samples, progress=progress) if samples else []
        )

        if ids and compute_metadata:
            dataset.select(ids).compute_metadata(progress=progress)

    _record_import(dataset, result)
    return ids


def check_compatible(dataset, fmt):
    """Raises a ``ValueError`` if ``dataset`` can't accept ``fmt`` data.

    A dataset holds either MCAP samples or LeRobot episodes, never both.

    Args:
        dataset: a :class:`fiftyone.core.dataset.Dataset`
        fmt: :const:`MCAP` or :const:`LEROBOT`
    """
    existing = dataset_format(dataset)
    if existing is not None and existing != fmt:
        raise ValueError(
            "Dataset '%s' already contains %s data and cannot also hold %s "
            "data. Import into a new dataset instead"
            % (dataset.name, _label(existing), _label(fmt))
        )

    if (
        existing is None
        and len(dataset) > 0
        and dataset.media_type != fo.core.media.MULTIMODAL
    ):
        raise ValueError(
            "Dataset '%s' contains %s samples. Import into a new or "
            "multimodal dataset instead" % (dataset.name, dataset.media_type)
        )


def dataset_format(dataset):
    """Returns the format of the data in ``dataset``, or ``None`` if empty.

    Args:
        dataset: a :class:`fiftyone.core.dataset.Dataset`

    Returns:
        :const:`MCAP`, :const:`LEROBOT`, or ``None``
    """
    fmt = (dataset.info.get(INFO_KEY) or {}).get("format", None)
    if fmt is not None:
        return fmt

    if dataset.info.get("lerobot"):
        return LEROBOT

    if len(dataset) > 0 and _is_mcap(dataset.first().filepath or ""):
        return MCAP

    return None


def username_for(email=None, name=None):
    """Returns a path-safe username for the given user.

    The local part of the email is preferred since it is unique and stable;
    the display name is used as a fallback.

    Args:
        email (None): the user's email
        name (None): the user's display name

    Returns:
        a path-safe string
    """
    raw = (email or "").split("@", 1)[0] or name or ""
    username = sanitize(raw)
    if not username:
        raise ValueError("Unable to determine a username for this user")

    return username


def sanitize(value):
    """Converts ``value`` into a lowercase, path-safe folder name.

    Args:
        value: a string

    Returns:
        the sanitized string, which may be empty
    """
    value = _UNSAFE_CHARS.sub("-", str(value).strip().lower())
    return value.strip(".-_")


def target_dir(root, username, dataset, template=None):
    """Returns the folder that new uploads for ``dataset`` should land in.

    Args:
        root: the root location, eg ``s3://bucket/fiftyone``
        username: the uploading user, see :func:`username_for`
        dataset: the dataset name
        template (None): a path template supporting ``{root}``,
            ``{username}``, and ``{dataset}``. The default is
            :const:`DEFAULT_PATH_TEMPLATE`

    Returns:
        the target folder
    """
    if not root:
        raise ValueError("A root location is required")

    dataset_dir = sanitize(dataset)
    if not dataset_dir:
        raise ValueError("Invalid dataset name '%s'" % dataset)

    path = (template or DEFAULT_PATH_TEMPLATE).format(
        root=fos.normalize_path(root).rstrip("/"),
        username=sanitize(username),
        dataset=dataset_dir,
    )
    return path.rstrip("/")


def is_within(path, root):
    """Whether ``path`` is ``root`` or lives below it.

    Args:
        path: a local or remote path
        root: a local or remote path

    Returns:
        True/False
    """
    path = fos.normalize_path(path).rstrip("/")
    root = fos.normalize_path(root).rstrip("/")
    return path == root or path.startswith(root + "/")


def upload_dir(local_dir, remote_dir, overwrite=False, progress=None):
    """Copies a local folder to a remote folder, preserving its layout.

    Files that already exist remotely with the same size are skipped unless
    ``overwrite`` is True, so an interrupted upload can simply be re-run.

    Args:
        local_dir: a local directory, or a single local file
        remote_dir: the destination directory
        overwrite (False): whether to re-upload files that already exist
        progress (None): an optional progress callback, as accepted by
            :func:`fiftyone.core.storage.copy_files`

    Returns:
        a tuple of ``(num_uploaded, num_skipped, bytes_uploaded)``
    """
    local_dir = os.path.abspath(os.path.expanduser(local_dir))
    if os.path.isfile(local_dir):
        relpaths = [os.path.basename(local_dir)]
        local_dir = os.path.dirname(local_dir)
    else:
        relpaths = [
            p
            for p in fos.list_files(local_dir, recursive=True)
            if not _is_hidden(p)
        ]

    if not relpaths:
        raise ValueError("No files found in '%s'" % local_dir)

    existing = {}
    if not overwrite and fos.isdir(remote_dir):
        for entry in fos.list_files(
            remote_dir, recursive=True, return_metadata=True
        ):
            existing[entry["filepath"]] = entry.get("size")

    inpaths, outpaths, skipped, num_bytes = [], [], 0, 0
    for relpath in relpaths:
        inpath = os.path.join(local_dir, relpath)
        size = os.path.getsize(inpath)
        key = relpath.replace(os.sep, "/")
        if existing.get(key) == size:
            skipped += 1
            continue

        inpaths.append(inpath)
        outpaths.append(fos.join(remote_dir, key))
        num_bytes += size

    if inpaths:
        fos.copy_files(inpaths, outpaths, progress=progress)

    return len(inpaths), skipped, num_bytes


def write_import_record(remote_dir, **fields):
    """Writes an ``_import.json`` record describing who uploaded what.

    Args:
        remote_dir: the upload folder
        **fields: fields to record, eg ``username``, ``dataset``, ``format``
    """
    record = dict(fields)
    record["uploaded_at"] = datetime.now(timezone.utc).isoformat()
    fos.write_json(
        record, fos.join(remote_dir, IMPORT_RECORD), pretty_print=True
    )


def format_bytes(num_bytes):
    """Formats a byte count for display, eg ``6.1 GB``."""
    num = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1000 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (num, unit)

        num /= 1000


def _record_import(dataset, result):
    info = dict(dataset.info.get(INFO_KEY) or {})
    sources = list(info.get("sources", []))
    if result.path not in sources:
        sources.append(result.path)

    info.update(format=result.format, sources=sources)
    dataset.info[INFO_KEY] = info
    dataset.save()


def _is_mcap(path):
    return os.path.splitext(path)[1].lower() in MCAP_EXTS


def _is_hidden(relpath):
    parts = relpath.replace(os.sep, "/").split("/")
    return any(p.startswith(".") for p in parts)


def _file_size(path):
    try:
        return fos.get_file_size(path) or 0
    except Exception:
        return 0


def _label(fmt):
    return {MCAP: "MCAP", LEROBOT: "LeRobot"}.get(fmt, str(fmt))
