"""
Storage-agnostic helpers shared by the multimodal I/O plugin and its CLI.

Everything here goes through :mod:`fiftyone.core.storage`, which picks the
backend (local, S3, GCS, Azure, MinIO) from the path, so the same code runs
against any bucket the caller has credentials for.

| Copyright 2026, Vinay Jakkali
| Licensed under the Apache License, Version 2.0
|
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import fnmatch
import os
import re

import yaml

import fiftyone as fo
import fiftyone.core.storage as fos
from fiftyone import ViewField as F


MCAP = "mcap"
LEROBOT = "lerobot"
FORMATS = (MCAP, LEROBOT)

MCAP_EXTS = (".mcap",)
SIDECAR_EXTS = (".yaml", ".yml")
MAX_SIDECAR_BYTES = 1000000
ROSBAG2_KEY = "rosbag2_bagfile_information"
LEROBOT_INFO_PATH = "meta/info.json"

DEFAULT_PATH_TEMPLATE = "{root}/users/{username}/{dataset}"
IMPORT_RECORD = "_import.json"
INFO_KEY = "multimodal_io"
UPLOADED_BY_FIELD = "uploaded_by"

# Fields that sidecars and key=value inputs may not set
RESERVED_FIELDS = {
    "id",
    "filepath",
    "tags",
    "metadata",
    "media_type",
    "created_at",
    "last_modified_at",
    "frames",
    "groups",
    UPLOADED_BY_FIELD,
}

_UNSAFE_CHARS = re.compile(r"[^a-z0-9._-]+")
_FIELD_UNSAFE_CHARS = re.compile(r"[^0-9A-Za-z_]+")
_INT = re.compile(r"^[+-]?(0|[1-9][0-9]*)$")
_FLOAT = re.compile(r"^[+-]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$")


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
        pattern: the glob pattern MCAP files were filtered by, if any
        sidecars: a dict mapping each MCAP filepath to the YAML sidecar files
            that apply to it, lowest precedence first. See
            :func:`match_sidecars`
    """

    path: str
    format: str = None
    files: list = field(default_factory=list)
    num_files: int = 0
    total_bytes: int = 0
    info: dict = field(default_factory=dict)
    pattern: str = None
    sidecars: dict = field(default_factory=dict)

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
            text = "MCAP: %d file%s, %s" % (
                self.num_files,
                "" if self.num_files == 1 else "s",
                format_bytes(self.total_bytes),
            )
            if self.sidecars:
                text += ", %d with YAML sidecars" % len(self.sidecars)

            return text

        return "No MCAP files or LeRobot dataset found"


def scan(path, pattern=None):
    """Detects whether ``path`` holds a LeRobot dataset or MCAP files.

    A directory containing ``meta/info.json`` is treated as a LeRobot v3
    dataset. Otherwise, all ``.mcap`` files at or below ``path`` are
    collected, optionally filtered by a glob ``pattern``. ``path`` may also
    be a single ``.mcap`` file.

    Args:
        path: a local or remote file or directory
        pattern (None): an optional glob pattern, relative to ``path``, that
            MCAP files must match, eg ``"**/kitchen_*.mcap"`` or
            ``"run1/*.mcap"``. Ignored for LeRobot datasets

    Returns:
        a :class:`ScanResult`
    """
    path = fos.normalize_path(path).rstrip("/")
    pattern = (pattern or "").strip() or None

    if _is_mcap(path) and fos.isfile(path):
        size = _file_size(path)
        folder = path.rsplit("/", 1)[0]
        neighbors = [
            fos.join(folder, p)
            for p in fos.list_files(folder)
            if not _is_hidden(p)
        ]
        return ScanResult(
            path=path,
            format=MCAP,
            files=[path],
            num_files=1,
            total_bytes=size,
            sidecars=match_sidecars(
                [path],
                [p for p in neighbors if _is_mcap(p)],
                [p for p in neighbors if is_sidecar(p)],
            ),
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

    files, all_mcaps, yamls, total = [], [], [], 0
    for entry in fos.list_files(path, recursive=True, return_metadata=True):
        relpath = entry["filepath"]
        if _is_hidden(relpath):
            continue

        # Sidecars are collected whatever the pattern, so a pattern that
        # selects MCAP files still picks up their metadata
        if is_sidecar(relpath):
            yamls.append(fos.join(path, relpath))
            continue

        if not _is_mcap(relpath):
            continue

        all_mcaps.append(fos.join(path, relpath))
        if pattern and not matches_pattern(relpath, pattern):
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
        pattern=pattern,
        sidecars=match_sidecars(files, all_mcaps, yamls),
    )


def is_sidecar(path):
    """Whether ``path`` is a YAML file that may hold sample metadata.

    Args:
        path: a path

    Returns:
        True/False
    """
    return os.path.splitext(path)[1].lower() in SIDECAR_EXTS


def match_sidecars(files, mcaps, yamls):
    """Pairs MCAP files with the YAML sidecar files that describe them.

    A YAML file applies to an MCAP file in the same folder when:

    -   its name matches the MCAP file, eg ``run1.yaml`` or ``run1.mcap.yaml``
        for ``run1.mcap``. These apply to that file only
    -   its name matches no MCAP file in the folder, eg ``metadata.yaml`` or
        ``annotations.yaml``. These apply to every MCAP file in the folder

    Folder-wide files come first, in name order, so a file's own sidecar
    wins when both set the same field.

    Args:
        files: the MCAP filepaths to match
        mcaps: every MCAP filepath in the same folders, used to tell
            per-file sidecars from folder-wide ones
        yamls: the YAML filepaths in the same folders

    Returns:
        a dict mapping each of ``files`` that has sidecars to a list of
        YAML filepaths
    """
    stems = defaultdict(set)
    for path in list(mcaps) + list(files):
        folder, name = _split(path)
        stems[folder].add(os.path.splitext(name)[0])

    by_folder = defaultdict(list)
    for path in sorted(yamls):
        folder, _ = _split(path)
        by_folder[folder].append(path)

    matches = {}
    for path in files:
        folder, name = _split(path)
        stem = os.path.splitext(name)[0]
        shared, own = [], []
        for yaml_path in by_folder.get(folder, []):
            yaml_stem = _sidecar_stem(yaml_path)
            if yaml_stem == stem:
                own.append(yaml_path)
            elif yaml_stem not in stems[folder]:
                shared.append(yaml_path)

        if shared or own:
            matches[path] = shared + own

    return matches


def read_sidecar(path):
    """Reads the sample fields from a YAML sidecar file.

    Nested keys are joined with ``_``, so ``robot: {name: ur5}`` becomes a
    ``robot_name`` field. Text, numbers, true/false, and dates are kept;
    lists and empty values are skipped.

    A rosbag2 ``metadata.yaml`` is recognized by its
    ``rosbag2_bagfile_information`` key. Its ``custom_data`` entries become
    fields, plus ``bag_duration_s``, ``bag_start_time``,
    ``bag_message_count``, and ``bag_ros_distro``.

    Args:
        path: a local or remote YAML filepath

    Returns:
        a tuple of ``(fields, warnings)``, where ``fields`` is a dict of
        field names to values and ``warnings`` is a list of strings
    """
    name = _split(path)[1]
    try:
        text = fos.read_file(path)
        if len(text) > MAX_SIDECAR_BYTES:
            return {}, [
                "Skipped %s: larger than %s"
                % (name, format_bytes(MAX_SIDECAR_BYTES))
            ]

        data = yaml.safe_load(text)
    except Exception as e:
        return {}, ["Skipped %s: %s" % (name, _first_line(e))]

    if data is None:
        return {}, []

    if not isinstance(data, dict):
        return {}, ["Skipped %s: expected key: value pairs" % name]

    warnings = []
    if isinstance(data.get(ROSBAG2_KEY), dict):
        fields = _rosbag2_fields(data[ROSBAG2_KEY], name, warnings)
    else:
        fields = flatten_fields(data, name, warnings)

    return fields, warnings


def flatten_fields(data, source=None, warnings=None, prefix=None):
    """Converts a nested dict into flat sample fields.

    Args:
        data: a dict
        source (None): where the data came from, for warnings
        warnings (None): an optional list to append warnings to
        prefix (None): a field name prefix

    Returns:
        a dict of field names to values
    """
    fields = {}
    for key, value in data.items():
        name = field_name(key if prefix is None else "%s_%s" % (prefix, key))
        if not name:
            _warn(warnings, source, "skipped key '%s': not a valid name" % key)
            continue

        if isinstance(value, dict):
            fields.update(flatten_fields(value, source, warnings, name))
        elif _is_field_value(value):
            fields[name] = value
        elif value is not None:
            _warn(
                warnings,
                source,
                "skipped '%s': lists and other nested values aren't "
                "supported" % name,
            )

    return fields


def field_name(key):
    """Converts a metadata key into a valid sample field name.

    Characters other than letters, digits, and ``_`` become ``_``, and names
    can't start with ``_`` or a digit.

    Args:
        key: a key

    Returns:
        a field name, or ``None`` if nothing usable is left
    """
    name = _FIELD_UNSAFE_CHARS.sub("_", str(key).strip())
    name = re.sub("_+", "_", name).strip("_")
    if not name:
        return None

    if name[0].isdigit():
        name = "f_" + name

    return name


def parse_value(text):
    """Infers the type of a value typed as text.

    ``true``/``false`` become booleans and plain numbers become ints or
    floats. Anything else, including numbers with leading zeros like
    ``007``, stays text.

    Args:
        text: a string

    Returns:
        a bool, int, float, or str
    """
    text = str(text).strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"

    if _INT.match(text):
        return int(text)

    if _FLOAT.match(text):
        return float(text)

    return text


def parse_fields(entries):
    """Parses ``key=value`` entries into sample fields.

    Args:
        entries: a list of ``key=value`` strings, or one string with an
            entry per line. Blank lines are ignored

    Returns:
        a dict of field names to values

    Raises:
        ValueError: if an entry is malformed or uses a reserved name
    """
    if isinstance(entries, str):
        entries = entries.splitlines()

    fields = {}
    for entry in entries or []:
        entry = (entry or "").strip()
        if not entry:
            continue

        key, sep, value = entry.partition("=")
        name = field_name(key)
        if not sep or not key.strip() or not name:
            raise ValueError(
                "Invalid field '%s'. Use key=value, eg robot=ur5" % entry
            )

        if name in RESERVED_FIELDS:
            raise ValueError(
                "'%s' is a built-in field and can't be set" % name
            )

        fields[name] = parse_value(value)

    return fields


def apply_metadata(dataset, ids, sidecars=None, fields=None):
    """Sets fields on samples from their YAML sidecars and ``fields``.

    ``fields`` (eg from key=value inputs) win over sidecar values.

    Args:
        dataset: a :class:`fiftyone.core.dataset.Dataset`
        ids: the sample IDs to update
        sidecars (None): a dict mapping filepaths to YAML sidecar paths, as
            in :attr:`ScanResult.sidecars`
        fields (None): a dict of field names to values to set on every
            sample

    Returns:
        a tuple of ``(field_names, warnings)``
    """
    if not ids or not (sidecars or fields):
        return [], []

    sample_ids, filepaths = dataset.select(ids).values(["id", "filepath"])

    cache, warnings, values = {}, [], {}
    for sample_id, filepath in zip(sample_ids, filepaths):
        sample_values = {}
        for yaml_path in (sidecars or {}).get(filepath, []):
            if yaml_path not in cache:
                cache[yaml_path], new_warnings = read_sidecar(yaml_path)
                warnings.extend(new_warnings)

            sample_values.update(cache[yaml_path])

        sample_values.update(fields or {})
        if sample_values:
            values[sample_id] = sample_values

    names, more_warnings = set_fields(dataset, values)
    return names, warnings + more_warnings


def set_fields(dataset, values):
    """Writes per-sample field values, reconciling their types.

    Values for an existing field are converted to its type when possible,
    and skipped otherwise. A new field whose values mix types is stored as
    floats (ints and floats) or text (anything else).

    Args:
        dataset: a :class:`fiftyone.core.dataset.Dataset`
        values: a dict mapping sample IDs to dicts of field values

    Returns:
        a tuple of ``(field_names, warnings)``
    """
    by_field = defaultdict(dict)
    for sample_id, sample_values in values.items():
        for name, value in sample_values.items():
            by_field[name][sample_id] = value

    schema = dataset.get_field_schema()
    names, warnings = [], []
    for name in sorted(by_field):
        if name in RESERVED_FIELDS or name.startswith("_"):
            warnings.append(
                "Skipped '%s': it's a built-in field and can't be set" % name
            )
            continue

        field_values = _reconcile(
            name, by_field[name], schema.get(name, None), warnings
        )
        if not field_values:
            continue

        ids = list(field_values)
        dataset.select(ids, ordered=True).set_values(
            name, [field_values[i] for i in ids]
        )
        names.append(name)

    return names, warnings


def matches_pattern(relpath, pattern):
    """Whether a relative path matches a glob pattern.

    Matching ignores case. ``*`` matches within a folder name, and a leading
    ``**/`` also matches files at the top level, so ``"**/*.mcap"`` matches
    ``"a.mcap"`` and ``"x/y/a.mcap"``.

    Args:
        relpath: a relative path using ``/`` separators
        pattern: a glob pattern

    Returns:
        True/False
    """
    relpath = relpath.replace(os.sep, "/").lower()
    pattern = pattern.lower()
    if fnmatch.fnmatchcase(relpath, pattern):
        return True

    while pattern.startswith("**/"):
        pattern = pattern[3:]
        if fnmatch.fnmatchcase(relpath, pattern):
            return True

    return False


def import_scan(
    dataset,
    result,
    tags=None,
    uploaded_by=None,
    compute_metadata=True,
    progress=None,
):
    """Adds the contents of a :class:`ScanResult` to ``dataset``.

    Re-running an import is safe: MCAP files already in the dataset are
    skipped, and a LeRobot dataset that was already imported from the same
    location is not added again.

    New samples record who brought them in, in an indexed
    :const:`UPLOADED_BY_FIELD` field, so the App can filter by user.

    Args:
        dataset: a :class:`fiftyone.core.dataset.Dataset`
        result: a :class:`ScanResult`
        tags (None): optional tag(s) to add to each new sample
        uploaded_by (None): the username to record on each new sample
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
        if result.path in imported_sources(dataset):
            return []

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

    if ids and uploaded_by:
        set_uploaded_by(dataset, ids, uploaded_by)

    _record_import(dataset, result)
    return ids


def refresh_existing(dataset, files, tags=None, uploaded_by=None):
    """Refreshes the samples for MCAP files that were replaced in place.

    When a file is re-uploaded over an existing object, its sample keeps the
    same filepath, so :func:`import_scan` skips it. This recomputes the
    sample's metadata from the new file (size, streams, and fingerprint),
    adds any ``tags``, and records ``uploaded_by`` as the latest uploader.

    Args:
        dataset: a :class:`fiftyone.core.dataset.Dataset`
        files: the replaced file paths
        tags (None): optional tag(s) to add to each refreshed sample
        uploaded_by (None): the username to record on each refreshed sample

    Returns:
        the list of refreshed sample IDs
    """
    if not files or not len(dataset):
        return []

    view = dataset.match(F("filepath").is_in(list(files)))
    ids = view.values("id")
    if not ids:
        return []

    view.compute_metadata(overwrite=True)

    if tags:
        view.tag_samples(list(tags))

    if uploaded_by:
        set_uploaded_by(dataset, ids, uploaded_by)

    return ids


def set_uploaded_by(dataset, ids, username):
    """Records ``username`` as the uploader of the given samples.

    The field is indexed so that filtering by user in the App stays fast on
    large datasets.

    Args:
        dataset: a :class:`fiftyone.core.dataset.Dataset`
        ids: the sample IDs
        username: the uploader's username
    """
    dataset.select(ids).set_values(UPLOADED_BY_FIELD, [username] * len(ids))
    if UPLOADED_BY_FIELD not in dataset.list_indexes():
        dataset.create_index(UPLOADED_BY_FIELD)


def imported_sources(dataset):
    """Returns the locations previously imported into ``dataset``.

    Args:
        dataset: a :class:`fiftyone.core.dataset.Dataset`

    Returns:
        a list of paths
    """
    return list((dataset.info.get(INFO_KEY) or {}).get("sources", []))


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


def upload_dir(
    local_dir, remote_dir, pattern=None, overwrite=False, progress=None
):
    """Copies a local folder to a remote folder, preserving its layout.

    Files that already exist remotely with the same size are skipped unless
    ``overwrite`` is True, so an interrupted upload can simply be re-run.

    Args:
        local_dir: a local directory, or a single local file
        remote_dir: the destination directory
        pattern (None): an optional glob pattern, relative to ``local_dir``,
            that files must match to be uploaded. See
            :func:`matches_pattern`
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
            and (not pattern or matches_pattern(p, pattern) or is_sidecar(p))
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


def _rosbag2_fields(info, source, warnings):
    fields = {}

    duration = (info.get("duration") or {}).get("nanoseconds", None)
    if _is_number(duration):
        fields["bag_duration_s"] = duration / 1e9

    start = (info.get("starting_time") or {}).get(
        "nanoseconds_since_epoch", None
    )
    if _is_number(start):
        fields["bag_start_time"] = datetime.fromtimestamp(
            start / 1e9, tz=timezone.utc
        )

    if _is_number(info.get("message_count", None)):
        fields["bag_message_count"] = info["message_count"]

    if info.get("ros_distro", None):
        fields["bag_ros_distro"] = str(info["ros_distro"])

    # rosbag2 stores custom data as text, so infer the types
    custom = info.get("custom_data", None) or {}
    if isinstance(custom, dict):
        custom = {
            k: parse_value(v) if isinstance(v, str) else v
            for k, v in custom.items()
        }
        fields.update(flatten_fields(custom, source, warnings))

    return fields


def _reconcile(name, values, existing, warnings):
    """Converts ``values`` (sample ID -> value) to one type, or returns None
    if the field can't be written."""
    if existing is not None:
        convert = _converter(existing)
        if convert is None:
            warnings.append(
                "Skipped '%s': the dataset already has a %s field with that "
                "name" % (name, type(existing).__name__)
            )
            return None

        result, skipped = {}, 0
        for sample_id, value in values.items():
            converted = convert(value)
            if converted is None:
                skipped += 1
            else:
                result[sample_id] = converted

        if skipped:
            warnings.append(
                "Skipped %d value(s) of '%s' that don't match its existing "
                "%s type" % (skipped, name, type(existing).__name__)
            )

        return result

    kinds = {_kind(v) for v in values.values()}
    if kinds <= {"int", "float"} and len(kinds) > 1:
        return {k: float(v) for k, v in values.items()}

    if kinds == {"date", "datetime"}:
        return {k: _to_datetime(v) for k, v in values.items()}

    if len(kinds) > 1:
        return {k: _to_str(v) for k, v in values.items()}

    return dict(values)


def _converter(field):
    if isinstance(field, fo.StringField):
        return _to_str

    if isinstance(field, fo.BooleanField):
        return lambda v: v if isinstance(v, bool) else None

    if isinstance(field, fo.IntField):
        return lambda v: (
            v
            if _kind(v) == "int"
            else int(v)
            if _kind(v) == "float" and v.is_integer()
            else None
        )

    if isinstance(field, fo.FloatField):
        return lambda v: float(v) if _kind(v) in ("int", "float") else None

    if isinstance(field, fo.DateTimeField):
        return lambda v: (
            _to_datetime(v) if _kind(v) in ("date", "datetime") else None
        )

    if isinstance(field, fo.DateField):
        return lambda v: (
            v.date()
            if _kind(v) == "datetime"
            else v
            if _kind(v) == "date"
            else None
        )

    return None


def _kind(value):
    if isinstance(value, bool):
        return "bool"

    if isinstance(value, int):
        return "int"

    if isinstance(value, float):
        return "float"

    if isinstance(value, datetime):
        return "datetime"

    if isinstance(value, date):
        return "date"

    return "str"


def _to_str(value):
    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, (date, datetime)):
        return value.isoformat()

    return str(value)


def _to_datetime(value):
    if isinstance(value, datetime):
        return value

    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _is_field_value(value):
    return isinstance(value, (bool, int, float, str, date, datetime))


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sidecar_stem(path):
    stem = os.path.splitext(_split(path)[1])[0]
    if _is_mcap(stem):
        stem = os.path.splitext(stem)[0]

    return stem


def _split(path):
    folder, _, name = path.replace(os.sep, "/").rpartition("/")
    return folder, name


def _warn(warnings, source, msg):
    if warnings is not None:
        warnings.append("%s: %s" % (source, msg) if source else msg)


def _first_line(e):
    text = str(e).strip()
    return text.splitlines()[0] if text else type(e).__name__


def _is_mcap(path):
    return os.path.splitext(path)[1].lower() in MCAP_EXTS


def _is_hidden(relpath):
    parts = relpath.replace(os.sep, "/").split("/")
    return any(p.startswith(".") for p in parts)


def _file_size(path):
    try:
        if fos.get_file_system(path) == fos.FileSystem.AZURE:
            # FiftyOne's get_file_size() doesn't work on Azure paths, but
            # listing the folder with metadata does
            folder, name = path.rsplit("/", 1)
            for entry in fos.list_files(folder, return_metadata=True):
                if entry["filepath"] == name:
                    return entry.get("size") or 0

            return 0

        return fos.get_file_size(path) or 0
    except Exception:
        return 0


def _label(fmt):
    return {MCAP: "MCAP", LEROBOT: "LeRobot"}.get(fmt, str(fmt))
