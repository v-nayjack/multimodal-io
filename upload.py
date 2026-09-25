"""
Uploads MCAP or LeRobot data to cloud storage and imports it into FiftyOne.

Use this for whole folders, LeRobot datasets, or scripted uploads. For MCAP
files from a laptop, the "Upload MCAP files" panel in the App also works.

Examples::

    # Upload a local folder to <root>/users/<you>/<dataset>, then import it
    python upload.py ./recordings --dataset kitchen-runs \\
        --root gs://my-bucket/fiftyone

    # Import data that is already in a bucket (no upload)
    python upload.py s3://my-bucket/fiftyone/shared/pick-place \\
        --dataset pick-place

    # Only upload/import MCAP files that match a glob pattern
    python upload.py ./recordings --dataset kitchen-runs \\
        --root gs://my-bucket/fiftyone --pattern "**/chopping*.mcap"

    # See what would happen without changing anything
    python upload.py ./recordings --dataset kitchen-runs --dry-run

The root and path template can also be set with the
``FIFTYONE_MULTIMODAL_IO_ROOT`` and ``FIFTYONE_MULTIMODAL_IO_PATH_TEMPLATE``
environment variables.

Bucket access happens from this machine. When connected to FiftyOne
Enterprise via ``FIFTYONE_API_URI`` and ``FIFTYONE_API_KEY``, the SDK uses the
cloud credentials stored on the deployment, if any; otherwise you need your
own bucket credentials. On deployments that only give the FiftyOne containers
bucket access, use the "Upload MCAP files" panel in the App instead.

| Copyright 2017-2026, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fiftyone as fo  # noqa: E402
import fiftyone.core.storage as fos  # noqa: E402

import core  # noqa: E402


ROOT_ENV = "FIFTYONE_MULTIMODAL_IO_ROOT"
TEMPLATE_ENV = "FIFTYONE_MULTIMODAL_IO_PATH_TEMPLATE"


def main(argv=None):
    """Runs the command line interface."""
    args = _parse_args(argv)

    source = args.source
    is_remote = not fos.is_local(source)
    if not is_remote:
        source = os.path.abspath(os.path.expanduser(source))
        if not os.path.exists(source):
            _fail("Local path '%s' does not exist" % source)

    local_scan = (
        core.scan(source, pattern=args.pattern) if not is_remote else None
    )
    if local_scan is not None and local_scan.format is None:
        _fail(_nothing_found(source, args.pattern))

    # Patterns only select MCAP files; a LeRobot dataset is uploaded whole
    upload_pattern = (
        args.pattern
        if local_scan is not None and local_scan.format == core.MCAP
        else None
    )

    # Fail before uploading anything if the dataset can't take this data
    if fo.dataset_exists(args.dataset) and local_scan is not None:
        core.check_compatible(fo.load_dataset(args.dataset), local_scan.format)

    username = args.username or _current_username()

    if is_remote:
        remote_dir = source
    else:
        root = args.root or os.environ.get(ROOT_ENV)
        if not root:
            _fail(
                "Provide --root (or set %s) so the upload has somewhere to "
                "go, eg --root gs://my-bucket/fiftyone" % ROOT_ENV
            )

        template = args.template or os.environ.get(TEMPLATE_ENV)
        remote_dir = core.target_dir(
            root, username, args.dataset, template=template
        )

        if os.path.isfile(source):
            remote_file = fos.join(remote_dir, os.path.basename(source))
        else:
            remote_file = None

    print("Source:      %s" % source)
    if local_scan is not None:
        print("Detected:    %s" % local_scan.describe())
        print("Upload to:   %s" % remote_dir)
    print(
        "Dataset:     %s (%s)" % (args.dataset, _dataset_state(args.dataset))
    )

    if args.dry_run:
        print("\nDry run: nothing was uploaded or imported")
        return 0

    if not is_remote:
        num_up, num_skip, num_bytes = core.upload_dir(
            source,
            remote_dir,
            pattern=upload_pattern,
            overwrite=args.overwrite,
            progress=True,
        )
        print(
            "Uploaded %d file(s), %s; skipped %d already uploaded"
            % (num_up, core.format_bytes(num_bytes), num_skip)
        )
        core.write_import_record(
            remote_dir,
            username=username,
            dataset=args.dataset,
            format=local_scan.format,
            source=os.path.basename(source.rstrip(os.sep)),
        )

    scan_path = remote_file if not is_remote and remote_file else remote_dir
    result = core.scan(scan_path, pattern=args.pattern)
    if result.format is None:
        _fail(_nothing_found(scan_path, args.pattern))

    if fo.dataset_exists(args.dataset):
        dataset = fo.load_dataset(args.dataset)
    else:
        dataset = fo.Dataset(args.dataset, persistent=True)

    ids = core.import_scan(
        dataset,
        result,
        tags=args.tags,
        uploaded_by=username,
        progress=True,
    )

    print("\nAdded %d sample(s) to '%s'" % (len(ids), dataset.name))
    print("Dataset now has %d sample(s)" % len(dataset))

    return 0


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Upload MCAP or LeRobot data to cloud storage and import it into "
            "a FiftyOne dataset"
        )
    )
    parser.add_argument(
        "source",
        help=(
            "a local folder or file to upload, or a bucket path (eg "
            "s3://bucket/path) to import in place"
        ),
    )
    parser.add_argument(
        "--dataset", required=True, help="the dataset to create or add to"
    )
    parser.add_argument(
        "--root",
        help="where uploads go, eg gs://bucket/fiftyone (default: $%s)"
        % ROOT_ENV,
    )
    parser.add_argument(
        "--template",
        help=(
            "upload path template using {root}, {username}, {dataset} "
            "(default: $%s or %s)" % (TEMPLATE_ENV, core.DEFAULT_PATH_TEMPLATE)
        ),
    )
    parser.add_argument(
        "--username",
        help=(
            "override the username used in the upload path and recorded in "
            "each sample's uploaded_by field"
        ),
    )
    parser.add_argument(
        "--tags", nargs="+", help="tag(s) to add to each new sample"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-upload files that already exist in the bucket",
    )
    parser.add_argument(
        "--pattern",
        help=(
            "only upload/import MCAP files matching this glob pattern, "
            'relative to the source, eg "**/chopping*.mcap" or "run1/*.mcap"'
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would happen without uploading or importing",
    )
    return parser.parse_args(argv)


def _current_username():
    try:
        import fiftyone.management as fom

        user = fom.whoami()
        return core.username_for(email=user.email, name=user.name)
    except Exception:
        return core.username_for(name=getpass.getuser())


def _dataset_state(name):
    if not fo.dataset_exists(name):
        return "will be created"

    return "exists, %d samples" % len(fo.load_dataset(name))


def _nothing_found(path, pattern):
    if pattern:
        return "No MCAP files matching '%s' found in '%s'" % (pattern, path)

    return "No MCAP files or LeRobot dataset found in '%s'" % path


def _fail(msg):
    print("Error: %s" % msg, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as e:
        _fail(str(e))
