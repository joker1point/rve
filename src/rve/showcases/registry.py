"""案例注册表：读取**包内** cases/<case_id>/case.json。

约定：
- 配方随包分发（hatchling 把包内 .json 打进 wheel），用 importlib.resources 定位，
  **不依赖仓库根目录** —— 安装后的用户也能 `rve showcase list`。
- 单个坏 case.json 不能让整个列表失败：跳过它并留下告警（list_cases_with_warnings）。
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from .models import ShowcaseCase

_CASES_DIRNAME = "cases"
_CASE_FILENAME = "case.json"


def _cases_root() -> Any:
    """定位包内 cases 目录（返回 Traversable；测试可替换为任意目录）。"""
    return resources.files("rve.showcases").joinpath(_CASES_DIRNAME)


def _case_files(root: Any) -> list[tuple[str, Any]]:
    """列出 (case_id, case.json 路径) 对，按 case_id 排序。

    目录不存在时返回空列表（不抛异常）——包被裁剪过也不该让调用方崩。
    """
    try:
        children = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []
    found: list[tuple[str, Any]] = []
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:  # 目录项不可读（如权限问题）：跳过，不拖垮整个列表
            continue
        path = child.joinpath(_CASE_FILENAME)
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        found.append((child.name, path))
    return sorted(found, key=lambda item: item[0])


def list_cases_with_warnings() -> tuple[list[ShowcaseCase], list[str]]:
    """列出全部案例与解析告警。

    坏 case.json（JSON 语法错 / 字段不符）会被跳过，并产出一条告警——
    一个坏文件绝不拖垮整个列表。
    """
    cases: list[ShowcaseCase] = []
    warnings: list[str] = []
    for case_id, path in _case_files(_cases_root()):
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            cases.append(ShowcaseCase.model_validate(data))
        except Exception as exc:  # noqa: BLE001 — 任何坏文件都只算"这一条坏了"
            warnings.append(
                f"案例 {case_id} 解析失败，已跳过：{type(exc).__name__}: {exc}"
            )
    return cases, warnings


def list_cases() -> list[ShowcaseCase]:
    """列出全部案例（按 id 排序）。坏 case.json 被静默跳过。"""
    cases, _ = list_cases_with_warnings()
    return cases


def load_case(case_id: str) -> ShowcaseCase:
    """按 id 读取单个案例。

    案例不存在时抛 FileNotFoundError；case.json 内容不合法时抛校验异常——
    两种失败都显式可见，绝不返回空壳。
    """
    for cid, path in _case_files(_cases_root()):
        if cid != case_id:
            continue
        raw = path.read_text(encoding="utf-8")
        return ShowcaseCase.model_validate_json(raw)
    raise FileNotFoundError(f"案例不存在: {case_id}")
