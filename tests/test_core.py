"""
Unit tests for the storage-agnostic helpers in ``multimodal_io/core.py``.

These use local folders only, so they need no cloud credentials or database.

| Copyright 2017-2026, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
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
