"""workspace 读写与列举的单元测试。"""

from __future__ import annotations

import pytest

from rve import workspace
from rve.schema.editspec import EditSpec, Shot, VideoSource


def _source() -> VideoSource:
    return VideoSource(
        path="x.mp4", duration_sec=10.0, fps=30.0, width=1920, height=1080
    )


def _spec(spec_id: str) -> EditSpec:
    return EditSpec(
        spec_id=spec_id,
        source=_source(),
        shots=[Shot(index=0, in_sec=0.0, out_sec=10.0)],
    )


@pytest.fixture
def workspaces(tmp_path, monkeypatch):
    monkeypatch.setenv("RVE_WORKSPACES", str(tmp_path))
    return tmp_path


def test_spec_dir_under_env(workspaces) -> None:
    assert workspace.spec_dir("abc") == workspaces / "abc"


def test_write_read_roundtrip(workspaces) -> None:
    spec = _spec("s1")
    path = workspace.write_spec(spec)
    assert path == workspaces / "s1" / "spec.json"
    assert path.exists()

    loaded = workspace.read_spec("s1")
    assert loaded.spec_id == "s1"
    assert loaded.model_dump() == spec.model_dump()


def test_list_specs(workspaces) -> None:
    workspace.write_spec(_spec("b2"))
    workspace.write_spec(_spec("a1"))
    assert workspace.list_specs() == ["a1", "b2"]


def test_list_specs_empty_when_no_root(workspaces) -> None:
    # 根目录尚未创建时返回空列表，不抛错
    assert workspace.list_specs() == []


def test_receipt_dir_created(workspaces) -> None:
    directory = workspace.receipt_dir("s1")
    assert directory.exists() and directory.is_dir()
    assert directory.name == "receipts"


def test_read_missing_raises(workspaces) -> None:
    with pytest.raises(FileNotFoundError):
        workspace.read_spec("nope")
