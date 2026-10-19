"""工作区：EditSpec 与回执的落盘位置与读写。

工作区根由环境变量 RVE_WORKSPACES 指定，缺省 ~/.rve/workspaces。
每个 spec 一个目录，内含 spec.json 与 receipts/。
"""

from __future__ import annotations

import os
from pathlib import Path

from .schema.editspec import EditSpec

SPEC_FILENAME = "spec.json"
RECEIPT_DIRNAME = "receipts"


def _base_dir() -> Path:
    """工作区根目录。"""
    env = os.environ.get("RVE_WORKSPACES")
    if env:
        return Path(env)
    return Path.home() / ".rve" / "workspaces"


def spec_dir(spec_id: str) -> Path:
    """返回某个 spec 的专属目录（不创建）。"""
    return _base_dir() / spec_id


def write_spec(spec: EditSpec) -> Path:
    """把 EditSpec 写为 spec.json 并返回路径。"""
    directory = spec_dir(spec.spec_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SPEC_FILENAME
    path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    return path


def read_spec(spec_id: str) -> EditSpec:
    """读取并校验 spec.json。文件不存在时抛 FileNotFoundError。"""
    path = spec_dir(spec_id) / SPEC_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"spec 不存在: {path}")
    return EditSpec.model_validate_json(path.read_text(encoding="utf-8"))


def list_specs() -> list[str]:
    """列出含 spec.json 的 spec_id（按名称排序）。"""
    base = _base_dir()
    if not base.exists():
        return []
    return sorted(
        child.name
        for child in base.iterdir()
        if child.is_dir() and (child / SPEC_FILENAME).is_file()
    )


def receipt_dir(spec_id: str) -> Path:
    """返回回执目录并确保已创建。"""
    directory = spec_dir(spec_id) / RECEIPT_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory
