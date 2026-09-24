"""
Multimodal I/O operators.

Browse any cloud bucket the deployment can reach and turn MCAP recordings or
LeRobot datasets into FiftyOne datasets, or upload a small MCAP file straight
from the browser.

| Copyright 2017-2026, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""

import os

import fiftyone as fo
import fiftyone.core.storage as fos
import fiftyone.operators as foo
import fiftyone.operators.types as types

try:
    from . import core, uploads
except ImportError:
    # Imported outside of FiftyOne's plugin loader, eg by pytest
    import core
    import uploads


ROOT_SECRET = "FIFTYONE_MULTIMODAL_IO_ROOT"
TEMPLATE_SECRET = "FIFTYONE_MULTIMODAL_IO_PATH_TEMPLATE"


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
        roots = _get_roots(ctx)

        if roots:
            inputs.view(
                "root_notice",
                types.Notice(
                    label="Imports are limited to: %s" % ", ".join(roots),
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

        if not _is_allowed(source, roots):
            prop.invalid = True
            prop.error_message = "Choose a folder inside: %s" % ", ".join(
                roots
            )
            return _form(inputs, "Import MCAP or LeRobot data")

        pattern_prop = inputs.str(
            "pattern",
            required=False,
            label="File pattern (optional)",
            description=(
                "Only import MCAP files matching this pattern, relative to "
                "the folder. Subfolders are always searched. Examples: "
                "**/chopping*.mcap, run1/*.mcap"
            ),
        )
        pattern = ctx.params.get("pattern", None)

        result = core.scan(source, pattern=pattern)
        if result.format is None:
            if pattern:
                pattern_prop.invalid = True
                pattern_prop.error_message = "No MCAP files match this pattern"
            else:
                prop.invalid = True
                prop.error_message = "No MCAP files or LeRobot dataset found"

            return _form(inputs, "Import MCAP or LeRobot data")

        prop.view.caption = "Detected " + result.describe()
        if pattern and result.format == core.LEROBOT:
            pattern_prop.view = types.View(
                caption="Ignored: LeRobot datasets are imported whole"
            )

        default_name = core.sanitize(os.path.basename(source.rstrip("/")))
        if not _dataset_inputs(ctx, inputs, result.format, default_name):
            return _form(inputs, "Import MCAP or LeRobot data")

        _options_inputs(inputs, result.format)

        return _form(inputs, "Import MCAP or LeRobot data")

    def execute(self, ctx):
        source = _parse_path(ctx, "source")
        roots = _get_roots(ctx)
        if not _is_allowed(source, roots):
            raise ValueError(
                "'%s' is outside the allowed locations: %s"
                % (source, ", ".join(roots))
            )

        result = core.scan(source, pattern=ctx.params.get("pattern", None))
        dataset = _get_target_dataset(ctx)

        ids = core.import_scan(
            dataset,
            result,
            tags=ctx.params.get("tags", None) or None,
            compute_metadata=True,
            progress=_progress(ctx),
        )

        _finish(ctx, dataset, result)

        return {"dataset": dataset.name, "num_added": len(ids)}

    def resolve_output(self, ctx):
        outputs = types.Object()
        outputs.str("dataset", label="Dataset")
        outputs.int("num_added", label="Samples added")
        return types.Property(outputs)


PANEL_NAME = "MultimodalUploadPanel"


class UploadMultimodal(foo.Operator):
    @property
    def config(self):
        return foo.OperatorConfig(
            name="upload_multimodal",
            label="Upload MCAP files",
            light_icon="/assets/icon-light.svg",
            dark_icon="/assets/icon-dark.svg",
        )

    def execute(self, ctx):
        if ctx.dataset is None:
            raise ValueError(
                "Open any dataset first; the upload panel opens inside a "
                "dataset but can upload into a different or new one"
            )

        ctx.trigger(
            "open_panel",
            params=dict(name=PANEL_NAME, isActive=True, layout="horizontal"),
        )


class GetUploadInfo(foo.Operator):
    @property
    def config(self):
        return foo.OperatorConfig(name="get_upload_info", unlisted=True)

    def execute(self, ctx):
        roots = _get_roots(ctx)
        info = {"roots": roots, "username": None, "target_dir": None}
        if not roots:
            info["error"] = (
                "Uploads are not configured. An admin needs to set the %s "
                "plugin secret to one or more comma-separated bucket "
                "folders, eg gs://my-bucket/fiftyone" % ROOT_SECRET
            )
            return info

        info["username"] = _get_username(ctx)
        try:
            info["root"] = _selected_root(ctx)
        except ValueError as e:
            info["error"] = str(e)
            return info

        name = (ctx.params.get("dataset_name", None) or "").strip()
        if not name:
            return info

        try:
            info["target_dir"] = _upload_dir(ctx, name)
        except ValueError as e:
            info["error"] = str(e)
            return info

        if fo.dataset_exists(name):
            dataset = fo.load_dataset(name)
            try:
                core.check_compatible(dataset, core.MCAP)
                info[
                    "dataset_state"
                ] = "Adds to existing dataset (%d samples)" % len(dataset)
            except ValueError as e:
                info["error"] = str(e)
        else:
            info["dataset_state"] = "Creates a new dataset"

        return info


class StartLargeUpload(foo.Operator):
    @property
    def config(self):
        return foo.OperatorConfig(name="start_large_upload", unlisted=True)

    def execute(self, ctx):
        path, size = _upload_target(ctx)

        # A file that is already fully uploaded only needs importing
        if fos.isfile(path) and fos.get_file_size(path) == size:
            return {"mode": "exists", "path": path}

        plan = uploads.start_upload(
            path, size, origin=ctx.params.get("origin", None)
        )
        plan["path"] = path
        return plan


class ResumeLargeUpload(foo.Operator):
    @property
    def config(self):
        return foo.OperatorConfig(name="resume_large_upload", unlisted=True)

    def execute(self, ctx):
        path, size = _upload_target(ctx)
        plan = uploads.resume_upload(path, size, ctx.params["upload_id"])
        plan["path"] = path
        return plan


class CompleteLargeUpload(foo.Operator):
    @property
    def config(self):
        return foo.OperatorConfig(name="complete_large_upload", unlisted=True)

    def execute(self, ctx):
        path, size = _upload_target(ctx)
        mode = ctx.params["mode"]
        if mode != "exists":
            uploads.complete_upload(
                path,
                mode,
                upload_id=ctx.params.get("upload_id", None),
                parts=ctx.params.get("parts", None),
            )

        actual = fos.get_file_size(path)
        if actual != size:
            raise ValueError(
                "Uploaded file is %d bytes but %d were expected"
                % (actual, size)
            )

        name = ctx.params["dataset_name"].strip()
        if fo.dataset_exists(name):
            dataset = fo.load_dataset(name)
        else:
            dataset = fo.Dataset(name, persistent=True)

        dest = _upload_dir(ctx, name)
        core.write_import_record(
            dest,
            username=_get_username(ctx),
            dataset=name,
            format=core.MCAP,
            source=os.path.basename(path),
        )

        ids = core.import_scan(
            dataset,
            core.scan(path),
            tags=ctx.params.get("tags", None) or None,
            compute_metadata=True,
        )
        return {"dataset": dataset.name, "num_added": len(ids), "path": path}


class AbortLargeUpload(foo.Operator):
    @property
    def config(self):
        return foo.OperatorConfig(name="abort_large_upload", unlisted=True)

    def execute(self, ctx):
        path, _ = _upload_target(ctx)
        uploads.abort_upload(
            path,
            ctx.params.get("mode", None),
            upload_id=ctx.params.get("upload_id", None),
        )
        return {"aborted": True}


class FinishUploadBatch(foo.Operator):
    @property
    def config(self):
        return foo.OperatorConfig(name="finish_upload_batch", unlisted=True)

    def execute(self, ctx):
        name = ctx.params.get("dataset_name", None)
        if not name or not fo.dataset_exists(name):
            return

        if ctx.dataset is not None and ctx.dataset.name == name:
            ctx.trigger("reload_dataset")
        else:
            ctx.trigger("open_dataset", dict(dataset=name))


def _upload_dir(ctx, dataset_name):
    root = _selected_root(ctx)
    return core.target_dir(
        root,
        _get_username(ctx),
        dataset_name,
        template=ctx.secret(TEMPLATE_SECRET),
    )


def _upload_target(ctx):
    """Recomputes the upload path server-side so a browser can only ever
    write into its own user's folder under the configured root."""
    filename = os.path.basename(ctx.params["filename"])
    if not filename.lower().endswith(core.MCAP_EXTS):
        raise ValueError("Only .mcap files can be uploaded")

    name = (ctx.params.get("dataset_name", None) or "").strip()
    if not name:
        raise ValueError("A dataset name is required")

    if fo.dataset_exists(name):
        core.check_compatible(fo.load_dataset(name), core.MCAP)

    size = int(ctx.params["size"])
    if size <= 0:
        raise ValueError("'%s' is empty" % filename)

    return fos.join(_upload_dir(ctx, name), filename), size


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


def _get_roots(ctx):
    """The allowed bucket folders, from a comma or newline separated
    secret."""
    value = ctx.secret(ROOT_SECRET) or ""
    roots = []
    for root in value.replace("\n", ",").split(","):
        root = root.strip().rstrip("/")
        if root and root not in roots:
            roots.append(root)

    return roots


def _is_allowed(path, roots):
    return not roots or any(core.is_within(path, r) for r in roots)


def _selected_root(ctx):
    """The upload root chosen in the panel, which must be an allowed one.
    Defaults to the first allowed root."""
    roots = _get_roots(ctx)
    if not roots:
        raise ValueError("The %s plugin secret is not set" % ROOT_SECRET)

    root = (ctx.params.get("root", None) or "").strip().rstrip("/")
    if not root:
        return roots[0]

    if root not in roots:
        raise ValueError(
            "'%s' is not an allowed upload location. Choose one of: %s"
            % (root, ", ".join(roots))
        )

    return root


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
    p.register(GetUploadInfo)
    p.register(StartLargeUpload)
    p.register(ResumeLargeUpload)
    p.register(CompleteLargeUpload)
    p.register(AbortLargeUpload)
    p.register(FinishUploadBatch)
