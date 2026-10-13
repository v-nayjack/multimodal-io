"""
Unit tests for the storage-agnostic helpers in ``multimodal_io/core.py``.

These use local folders only, so they need no cloud credentials or database.

| Copyright 2026, Vinay Jakkali
| Licensed under the Apache License, Version 2.0
|
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core  # noqa: E402


def _touch(path, num_bytes=10):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * num_bytes)


def test_sanitize():
    assert core.sanitize("Pick & Place v2") == "pick-place-v2"
    assert core.sanitize("  ..hidden") == "hidden"
    assert core.sanitize("a/b\\c") == "a-b-c"
    assert core.sanitize("---") == ""


def test_username_for():
    assert core.username_for(email="Jane.Doe@acme.com") == "jane.doe"
    assert core.username_for(name="Jane Doe") == "jane-doe"
    with pytest.raises(ValueError):
        core.username_for()


def test_target_dir():
    assert (
        core.target_dir("gs://bkt/fiftyone/", "jane", "My Robot Data")
        == "gs://bkt/fiftyone/users/jane/my-robot-data"
    )
    assert (
        core.target_dir(
            "s3://bkt", "jane", "run1", template="{root}/{dataset}/{username}"
        )
        == "s3://bkt/run1/jane"
    )
    with pytest.raises(ValueError):
        core.target_dir("s3://bkt", "jane", "///")

    with pytest.raises(ValueError):
        core.target_dir(None, "jane", "run1")


def test_is_within():
    assert core.is_within("s3://bkt/root/users/a", "s3://bkt/root")
    assert core.is_within("s3://bkt/root", "s3://bkt/root/")
    assert not core.is_within("s3://bkt/rootx", "s3://bkt/root")
    assert not core.is_within("gs://bkt/root/a", "s3://bkt/root")


def test_scan_mcap(tmp_path):
    _touch(str(tmp_path / "a.mcap"), 100)
    _touch(str(tmp_path / "sub" / "b.MCAP"), 50)
    _touch(str(tmp_path / "notes.txt"))
    _touch(str(tmp_path / ".cache" / "c.mcap"))

    result = core.scan(str(tmp_path))

    assert result.format == core.MCAP
    assert result.num_files == 2
    assert result.total_bytes == 150
    assert [os.path.basename(f) for f in result.files] == ["a.mcap", "b.MCAP"]
    assert result.describe() == "MCAP: 2 files, 150 B"


def test_scan_single_mcap(tmp_path):
    path = str(tmp_path / "one.mcap")
    _touch(path, 2048)

    result = core.scan(path)

    assert result.format == core.MCAP
    assert result.files == [path]
    assert result.total_bytes == 2048


def test_scan_lerobot(tmp_path):
    info = {
        "codebase_version": "v3.0",
        "total_episodes": 50,
        "total_frames": 11939,
        "robot_type": "so101",
    }
    os.makedirs(tmp_path / "meta")
    with open(tmp_path / "meta" / "info.json", "w") as f:
        json.dump(info, f)

    # MCAP files inside a LeRobot tree don't change the detected format
    _touch(str(tmp_path / "extra.mcap"))

    result = core.scan(str(tmp_path))

    assert result.format == core.LEROBOT
    assert result.info["total_episodes"] == 50
    assert (
        result.describe() == "LeRobot v3.0: 50 episodes, 11939 frames, so101"
    )


def test_scan_empty(tmp_path):
    _touch(str(tmp_path / "readme.md"))

    result = core.scan(str(tmp_path))

    assert result.format is None
    assert result.num_files == 0


def test_upload_dir_resumes(tmp_path):
    src = tmp_path / "src"
    dst = str(tmp_path / "dst")
    _touch(str(src / "a.mcap"), 10)
    _touch(str(src / "nested" / "b.mcap"), 20)
    _touch(str(src / ".DS_Store"), 5)

    assert core.upload_dir(str(src), dst) == (2, 0, 30)
    assert os.path.isfile(os.path.join(dst, "nested", "b.mcap"))
    assert not os.path.exists(os.path.join(dst, ".DS_Store"))

    # Unchanged files are skipped on a re-run; changed files are re-sent
    _touch(str(src / "a.mcap"), 11)
    assert core.upload_dir(str(src), dst) == (1, 1, 11)

    assert core.upload_dir(str(src), dst, overwrite=True) == (2, 0, 31)


def test_upload_single_file(tmp_path):
    path = str(tmp_path / "one.mcap")
    _touch(path, 7)
    dst = str(tmp_path / "dst")

    assert core.upload_dir(path, dst) == (1, 0, 7)
    assert os.path.isfile(os.path.join(dst, "one.mcap"))


def test_import_record(tmp_path):
    core.write_import_record(
        str(tmp_path), username="jane", dataset="run1", format=core.MCAP
    )

    with open(tmp_path / core.IMPORT_RECORD) as f:
        record = json.load(f)

    assert record["username"] == "jane"
    assert record["format"] == "mcap"
    assert "uploaded_at" in record


def test_format_bytes():
    assert core.format_bytes(0) == "0 B"
    assert core.format_bytes(6_100_000_000) == "6.1 GB"
    assert core.format_bytes(None) == "0 B"


def test_matches_pattern():
    assert core.matches_pattern("a.mcap", "**/*.mcap")
    assert core.matches_pattern("x/y/a.mcap", "**/*.mcap")
    assert core.matches_pattern("x/chopping_1.mcap", "**/chopping*.mcap")
    assert core.matches_pattern("chopping_1.mcap", "**/chopping*.mcap")
    assert core.matches_pattern("run1/a.mcap", "run1/*.mcap")
    assert not core.matches_pattern("run2/a.mcap", "run1/*.mcap")
    assert not core.matches_pattern("x/sweeping.mcap", "**/chopping*.mcap")
    assert core.matches_pattern("Sub/Chopping.MCAP", "sub/chopping*.mcap")


def test_scan_pattern(tmp_path):
    _touch(str(tmp_path / "chopping.mcap"), 10)
    _touch(str(tmp_path / "run1" / "chopping_2.mcap"), 20)
    _touch(str(tmp_path / "run1" / "sweeping.mcap"), 30)
    _touch(str(tmp_path / "run2" / "sweeping.mcap"), 40)

    result = core.scan(str(tmp_path), pattern="**/chopping*.mcap")
    assert result.num_files == 2
    assert result.total_bytes == 30
    assert result.pattern == "**/chopping*.mcap"

    result = core.scan(str(tmp_path), pattern="run1/*.mcap")
    assert result.num_files == 2
    assert result.total_bytes == 50

    result = core.scan(str(tmp_path), pattern="**/missing*.mcap")
    assert result.format is None

    # A blank pattern means "everything"
    assert core.scan(str(tmp_path), pattern="  ").num_files == 4


def test_upload_dir_pattern(tmp_path):
    src = tmp_path / "src"
    dst = str(tmp_path / "dst")
    _touch(str(src / "keep.mcap"), 10)
    _touch(str(src / "sub" / "keep_2.mcap"), 20)
    _touch(str(src / "skip.mcap"), 30)

    assert core.upload_dir(str(src), dst, pattern="**/keep*.mcap") == (
        2,
        0,
        30,
    )
    assert not os.path.exists(os.path.join(dst, "skip.mcap"))


def test_part_size_for():
    import uploads

    mib = 1024 * 1024
    # Small and medium files use the 64 MiB minimum
    assert uploads.part_size_for(1) == 64 * mib
    assert uploads.part_size_for(6_712_135_583) == 64 * mib

    # Huge files get bigger parts so they never need more than 1000
    size = 200 * 1024**3
    part = uploads.part_size_for(size)
    assert part % mib == 0
    assert -(-size // part) <= uploads.MAX_PARTS


def test_azure_block_ids():
    import uploads

    nums = (1, 42, 999999)
    ids = [uploads.block_id(n) for n in nums]
    assert len({len(i) for i in ids}) == 1  # Azure needs equal-length IDs

    # The SDK takes and returns plain names; REST uses base64 IDs
    assert [uploads.block_number(uploads.block_name(n)) for n in nums] == [
        1,
        42,
        999999,
    ]
    assert [uploads.block_number(i) for i in ids] == [1, 42, 999999]
    assert uploads.block_id(1) == "MDAwMDAx"  # same as the browser's btoa
    assert uploads.block_number("not-ours") is None


def test_clients_path_parsing():
    import clients

    assert clients._split("bucket/a/b.mcap") == ("bucket", "a/b.mcap")
    assert clients._split("bucket") == ("bucket", "")

    m = clients._AZURE_URL.match(
        "https://acct1.blob.core.windows.net/container/users/jane/a.mcap"
    )
    assert m.groups() == ("acct1", "container", "users/jane/a.mcap")
    assert clients._AZURE_URL.match("az://container/a.mcap") is None


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


ROSBAG2_METADATA = """\
rosbag2_bagfile_information:
  version: 9
  storage_identifier: mcap
  duration:
    nanoseconds: 12500000000
  starting_time:
    nanoseconds_since_epoch: 1760000000000000000
  message_count: 4210
  topics_with_message_count:
    - topic_metadata:
        name: /camera/image/compressed
        type: sensor_msgs/msg/CompressedImage
      message_count: 375
  relative_file_paths:
    - run1_0.mcap
  custom_data:
    robot: ur5e
    episode: "12"
    validated: "true"
    operator_id: "007"
  ros_distro: humble
"""


def test_match_sidecars(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "a.mcap"))
    _touch(os.path.join(root, "b.mcap"))
    _write(os.path.join(root, "metadata.yaml"), "x: 1\n")
    _write(os.path.join(root, "a.yaml"), "x: 2\n")
    _write(os.path.join(root, "b.mcap.yml"), "x: 3\n")
    _write(os.path.join(root, "other", "metadata.yaml"), "x: 4\n")

    result = core.scan(root)
    names = {
        os.path.basename(f): [os.path.relpath(p, root) for p in paths]
        for f, paths in result.sidecars.items()
    }

    # Folder-wide files first, then the file's own sidecar
    assert names == {
        "a.mcap": ["metadata.yaml", "a.yaml"],
        "b.mcap": ["metadata.yaml", "b.mcap.yml"],
    }
    assert result.describe() == "MCAP: 2 files, 20 B, 2 with YAML sidecars"

    # A single file picks up the sidecars next to it, and a pattern that
    # selects MCAP files still finds them
    single = core.scan(os.path.join(root, "b.mcap"))
    assert len(single.sidecars[os.path.join(root, "b.mcap")]) == 2
    assert len(core.scan(root, pattern="a.mcap").sidecars) == 1


def test_read_sidecar(tmp_path):
    path = str(tmp_path / "annotations.yaml")
    _write(
        path,
        "robot:\n  name: ur5e\n  arm count: 2\n"
        "campaign: summer-24\nverdict: true\nscore: 0.9\n"
        "recorded: 2026-10-09\nlabels: [a, b]\nempty:\n",
    )

    fields, warnings = core.read_sidecar(path)

    assert fields["robot_name"] == "ur5e"
    assert fields["robot_arm_count"] == 2
    assert fields["campaign"] == "summer-24"
    assert fields["verdict"] is True
    assert fields["score"] == 0.9
    assert str(fields["recorded"]) == "2026-10-09"
    assert "labels" not in fields and "empty" not in fields
    assert len(warnings) == 1 and "labels" in warnings[0]


def test_read_rosbag2_metadata(tmp_path):
    path = str(tmp_path / "metadata.yaml")
    _write(path, ROSBAG2_METADATA)

    fields, warnings = core.read_sidecar(path)

    assert fields["robot"] == "ur5e"
    assert fields["episode"] == 12
    assert fields["validated"] is True
    assert fields["operator_id"] == "007"
    assert fields["bag_duration_s"] == 12.5
    assert fields["bag_message_count"] == 4210
    assert fields["bag_ros_distro"] == "humble"
    assert fields["bag_start_time"].year == 2025
    assert not any(k.startswith("rosbag2") for k in fields)
    assert warnings == []


def test_read_sidecar_bad_files(tmp_path):
    bad = str(tmp_path / "bad.yaml")
    _write(bad, "a: [unclosed\n")
    fields, warnings = core.read_sidecar(bad)
    assert fields == {} and "bad.yaml" in warnings[0]

    listing = str(tmp_path / "list.yaml")
    _write(listing, "- a\n- b\n")
    assert core.read_sidecar(listing)[0] == {}

    assert core.read_sidecar(str(tmp_path / "missing.yaml"))[0] == {}


def test_field_name():
    assert core.field_name("robot") == "robot"
    assert core.field_name("Robot Type") == "Robot_Type"
    assert core.field_name("a.b/c-d") == "a_b_c_d"
    assert core.field_name("_hidden") == "hidden"
    assert core.field_name("2nd_try") == "f_2nd_try"
    assert core.field_name("$$") is None


def test_parse_fields():
    assert core.parse_fields(["robot=ur5", " count = 3 ", "", "ok=true"]) == {
        "robot": "ur5",
        "count": 3,
        "ok": True,
    }
    assert core.parse_fields("ratio=0.5\nrun_id=007\nnote=a=b") == {
        "ratio": 0.5,
        "run_id": "007",
        "note": "a=b",
    }
    assert core.parse_fields(None) == {}

    for bad in (["robot"], ["=x"], ["tags=a"], ["uploaded_by=me"]):
        with pytest.raises(ValueError):
            core.parse_fields(bad)


def test_upload_dir_pattern_keeps_sidecars(tmp_path):
    src = tmp_path / "src"
    dst = str(tmp_path / "dst")
    _touch(str(src / "keep.mcap"), 10)
    _touch(str(src / "skip.mcap"), 30)
    _write(str(src / "metadata.yaml"), "x: 1\n")

    num_up, _, _ = core.upload_dir(str(src), dst, pattern="keep*.mcap")
    assert num_up == 2
    assert os.path.isfile(os.path.join(dst, "metadata.yaml"))


def test_safe_relpath():
    assert core.safe_relpath("run.mcap") == "run.mcap"
    assert core.safe_relpath("rec1/metadata.yaml") == "rec1/metadata.yaml"
    assert core.safe_relpath("a\\b.mcap") == "a/b.mcap"

    for bad in (
        "",
        "/abs.mcap",
        "../up.mcap",
        "a/../../b.mcap",
        "a//b.mcap",
        ".hidden/a.mcap",
        "a/./b.mcap",
        "x/" * 10 + "deep.mcap",
    ):
        with pytest.raises(ValueError):
            core.safe_relpath(bad)


def test_folder_sidecars_stay_in_their_folder(tmp_path):
    root = str(tmp_path)
    for rec in ("rec1", "rec2"):
        _touch(os.path.join(root, rec, "%s_0.mcap" % rec))
        _write(os.path.join(root, rec, "metadata.yaml"), "robot: %s\n" % rec)

    rec1 = os.path.join(root, "rec1")
    sidecars = core.folder_sidecars(rec1)
    assert sidecars == {
        os.path.join(rec1, "rec1_0.mcap"): [
            os.path.join(rec1, "metadata.yaml")
        ]
    }

    # Each recording reads only its own metadata.yaml
    result = core.scan(root)
    for path, yamls in result.sidecars.items():
        assert [os.path.dirname(y) for y in yamls] == [os.path.dirname(path)]
