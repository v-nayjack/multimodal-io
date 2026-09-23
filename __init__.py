"""
Multimodal I/O operators.

Browse any cloud bucket the deployment can reach and turn MCAP recordings or
LeRobot datasets into FiftyOne datasets, or upload a small MCAP file straight
from the browser.

| Copyright 2017-2026, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""

import base64
import os

import fiftyone as fo
import fiftyone.core.storage as fos
import fiftyone.operators as foo
import fiftyone.operators.types as types

try:
    from . import core
except ImportError:
    # Imported outside of FiftyOne's plugin loader, eg by pytest
    import core


ROOT_SECRET = "FIFTYONE_MULTIMODAL_IO_ROOT"
TEMPLATE_SECRET = "FIFTYONE_MULTIMODAL_IO_PATH_TEMPLATE"
MAX_UPLOAD_SECRET = "FIFTYONE_MULTIMODAL_IO_MAX_UPLOAD_MB"

DEFAULT_MAX_UPLOAD_MB = 100

NEW_DATASET = "NEW"
CURRENT_DATASET = "CURRENT"


class ImportMultimodal(foo.Operator):
    @property
    def config(self):
        return foo.OperatorConfig(
            name="import_multimodal",
            label="Import MCAP or LeRobot data",
            light_icon="/assets/icon-light.svg",
            dark_icon="/assets/icon-dark.svg",
            dynamic=True,
            allow_immediate_execution=True,
            allow_delegated_execution=True,
            default_choice_to_delegated=True,
        )

    def resolve_input(self, ctx):
        inputs = types.Object()
        root = _get_root(ctx)

        if root:
            inputs.view(
                "root_notice",
                types.Notice(
                    label="Imports are limited to %s" % root,
                ),
            )

        prop = inputs.file(
            "source",
            required=True,
            label="Folder",
            description=(
                "Choose a folder of MCAP files or a LeRobot dataset (the "
                "folder that contains meta/info.json)"
            ),
            view=types.FileExplorerView(
                choose_dir=True,
                button_label="Choose a folder...",
            ),
        )

        source = _parse_path(ctx, "source")
        if not source:
            return _form(inputs, "Import MCAP or LeRobot data")

        if root and not core.is_within(source, root):
            prop.invalid = True
            prop.error_message = "Choose a folder inside %s" % root
            return _form(inputs, "Import MCAP or LeRobot data")

        result = core.scan(source)
        if result.format is None:
            prop.invalid = True
            prop.error_message = "No MCAP files or LeRobot dataset found"
            return _form(inputs, "Import MCAP or LeRobot data")

        prop.view.caption = "Detected " + result.describe()

        default_name = core.sanitize(os.path.basename(source.rstrip("/")))
        if not _dataset_inputs(ctx, inputs, result.format, default_name):
            return _form(inputs, "Import MCAP or LeRobot data")

        _options_inputs(inputs, result.format)

        return _form(inputs, "Import MCAP or LeRobot data")

    def execute(self, ctx):
        source = _parse_path(ctx, "source")
        root = _get_root(ctx)
        if root and not core.is_within(source, root):
            raise ValueError("'%s' is outside %s" % (source, root))

        result = core.scan(source)
        dataset = _get_target_dataset(ctx)

        ids = core.import_scan(
            dataset,
            result,
            tags=ctx.params.get("tags", None) or None,
            compute_metadata=ctx.params.get("compute_metadata", True),
            progress=_progress(ctx),
        )

        _finish(ctx, dataset, result)

        return {"dataset": dataset.name, "num_added": len(ids)}

    def resolve_output(self, ctx):
        outputs = types.Object()
        outputs.str("dataset", label="Dataset")
        outputs.int("num_added", label="Samples added")
        return types.Property(outputs)


class UploadMultimodal(foo.Operator):
    @property
    def config(self):
        return foo.OperatorConfig(
            name="upload_multimodal",
            label="Upload an MCAP file",
            light_icon="/assets/icon-light.svg",
            dark_icon="/assets/icon-dark.svg",
            dynamic=True,
            allow_immediate_execution=True,
            allow_delegated_execution=False,
        )

    def resolve_input(self, ctx):
        inputs = types.Object()
        root = _get_root(ctx)
        max_mb = _get_max_upload_mb(ctx)

        if not root:
            inputs.view(
                "no_root",
                types.Error(
                    label="Uploads are not configured",
                    description=(
                        "An admin needs to set the %s plugin secret to the "
                        "bucket folder uploads should go to, eg "
                        "gs://my-bucket/fiftyone" % ROOT_SECRET
                    ),
                ),
            )
            return _form(inputs, "Upload an MCAP file")

        inputs.define_property(
            "file",
            types.UploadedFile(),
            required=True,
            label="MCAP file",
            description=(
                "Up to %d MB. For larger files, use the upload script "
                "(see the plugin README)" % max_mb
            ),
            view=types.FileView(
                label="MCAP file",
                types=".mcap",
                max_size=max_mb * 1024 * 1024,
                max_size_error_message=(
                    "This file is larger than %d MB. Use the upload script "
                    "for large files" % max_mb
                ),
                lite=True,
            ),
        )

        uploaded = ctx.params.get("file", None)
        if not uploaded:
            return _form(inputs, "Upload an MCAP file")

        filename = uploaded.get("name") or ""
        default_name = core.sanitize(os.path.splitext(filename)[0])
        if not _dataset_inputs(ctx, inputs, core.MCAP, default_name):
            return _form(inputs, "Upload an MCAP file")

        name = _target_dataset_name(ctx)
        try:
            dest = core.target_dir(
                root,
                _get_username(ctx),
                name,
                template=ctx.secret(TEMPLATE_SECRET),
            )
            inputs.view(
                "dest_notice",
                types.Notice(label="Saves to %s/%s" % (dest, filename)),
            )
        except ValueError as e:
            inputs.view("dest_error", types.Error(label=str(e)))
            return _form(inputs, "Upload an MCAP file")

        _options_inputs(inputs, core.MCAP)

        return _form(inputs, "Upload an MCAP file")

    def execute(self, ctx):
        root = _get_root(ctx)
        if not root:
            raise ValueError("The %s plugin secret is not set" % ROOT_SECRET)

        uploaded = ctx.params["file"]
        filename = os.path.basename(uploaded["name"])
        if not filename.lower().endswith(core.MCAP_EXTS):
            raise ValueError("Only .mcap files can be uploaded")

        content = base64.b64decode(uploaded["content"])
        max_mb = _get_max_upload_mb(ctx)
        if len(content) > max_mb * 1024 * 1024:
            raise ValueError("File is larger than %d MB" % max_mb)

        username = _get_username(ctx)
        name = _target_dataset_name(ctx)
        dest = core.target_dir(
            root, username, name, template=ctx.secret(TEMPLATE_SECRET)
        )
        filepath = fos.join(dest, filename)

        # Validate the target before writing anything to the bucket
        dataset = _get_target_dataset(ctx, create=False)
        if dataset is not None:
            core.check_compatible(dataset, core.MCAP)

        fos.write_file(content, filepath)
        core.write_import_record(
            dest,
            username=username,
            dataset=name,
            format=core.MCAP,
            source=filename,
        )

        if dataset is None:
            dataset = _get_target_dataset(ctx)

        result = core.scan(filepath)
        ids = core.import_scan(
            dataset,
            result,
            tags=ctx.params.get("tags", None) or None,
            compute_metadata=ctx.params.get("compute_metadata", True),
        )

        _finish(ctx, dataset, result)

        return {"dataset": dataset.name, "num_added": len(ids)}

    def resolve_output(self, ctx):
        outputs = types.Object()
        outputs.str("dataset", label="Dataset")
        outputs.int("num_added", label="Samples added")
        return types.Property(outputs)


def _dataset_inputs(ctx, inputs, fmt, default_name):
    choices = types.RadioGroup(orientation="horizontal")
    choices.add_choice(NEW_DATASET, label="New dataset")

    current_error = None
    if ctx.dataset is not None:
        try:
            core.check_compatible(ctx.dataset, fmt)
            choices.add_choice(
                CURRENT_DATASET,
                label="Add to %s" % ctx.dataset.name,
            )
        except ValueError as e:
            current_error = str(e)

    inputs.enum(
        "destination",
        choices.values(),
        default=NEW_DATASET,
        required=True,
        label="Destination",
        view=choices,
    )

    if current_error:
        inputs.view(
            "current_notice",
            types.Notice(
                label="Can't add to the current dataset: %s" % current_error
            ),
        )

    if ctx.params.get("destination", NEW_DATASET) != NEW_DATASET:
        return True

    prop = inputs.str(
        "dataset_name",
        required=True,
        default=default_name or None,
        label="Dataset name",
    )

    name = ctx.params.get("dataset_name", None) or default_name
    if not name:
        return False

    if fo.dataset_exists(name):
        prop.invalid = True
        prop.error_message = "A dataset named '%s' already exists" % name
        return False

    return True


def _options_inputs(inputs, fmt):
    inputs.list(
        "tags",
        types.String(),
        default=None,
        label="Tags",
        description="Optional tag(s) to add to each new sample",
        view=types.AutocompleteView(multiple=True),
    )

    if fmt == core.MCAP:
        inputs.bool(
            "compute_metadata",
            default=True,
            label="Compute metadata",
            description=(
                "Read each MCAP's topics, time range, and message counts"
            ),
            view=types.CheckboxView(),
        )


def _target_dataset_name(ctx):
    if ctx.params.get("destination", NEW_DATASET) == CURRENT_DATASET:
        return ctx.dataset.name

    return ctx.params.get("dataset_name", None)


def _get_target_dataset(ctx, create=True):
    if ctx.params.get("destination", NEW_DATASET) == CURRENT_DATASET:
        return ctx.dataset

    name = ctx.params.get("dataset_name", None)
    if not name:
        raise ValueError("A dataset name is required")

    if fo.dataset_exists(name):
        raise ValueError("A dataset named '%s' already exists" % name)

    if not create:
        return None

    return fo.Dataset(name, persistent=True)


def _finish(ctx, dataset, result):
    if ctx.delegated:
        return

    if ctx.dataset is not None and ctx.dataset.name == dataset.name:
        ctx.trigger("reload_dataset")
    else:
        ctx.trigger("open_dataset", dict(dataset=dataset.name))


def _progress(ctx):
    if not ctx.delegated:
        return None

    return fo.report_progress(
        lambda pb: ctx.set_progress(progress=pb.progress), dt=5.0
    )


def _get_root(ctx):
    root = ctx.secret(ROOT_SECRET)
    return root.strip().rstrip("/") if root else None


def _get_max_upload_mb(ctx):
    try:
        return int(ctx.secret(MAX_UPLOAD_SECRET) or DEFAULT_MAX_UPLOAD_MB)
    except ValueError:
        return DEFAULT_MAX_UPLOAD_MB


def _get_username(ctx):
    user = ctx.user
    email = getattr(user, "email", None)
    name = getattr(user, "name", None)

    # Operators run outside the App (eg via the SDK) may not carry a user
    if not email and not name:
        import fiftyone.management as fom

        whoami = fom.whoami()
        email, name = whoami.email, whoami.name

    return core.username_for(email=email, name=name)


def _parse_path(ctx, key):
    value = ctx.params.get(key, None)
    if isinstance(value, dict):
        return value.get("absolute_path", None)

    return value or None


def _form(inputs, label):
    return types.Property(inputs, view=types.View(label=label))


def register(p):
    p.register(ImportMultimodal)
    p.register(UploadMultimodal)
