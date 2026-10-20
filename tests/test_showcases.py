"""案例库（showcases）测试。

覆盖：
- 注册表：能列出案例 / 逐条通过模型校验 / 不存在的 id 抛 FileNotFoundError /
  坏 case.json 不拖垮整个列表；
- 素材：程序合成能落地、缺外部素材时抛 MaterialMissing 且消息带"怎么准备"指引；
- 执行器：合成案例端到端跑通（ok=True）、期望不符时判 fail（绝不 quietly succeed）。

本机缺 ffmpeg 时，依赖 ffmpeg 的用例显式 skip 并说明原因——不假装通过。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from rve.showcases import (
    MaterialMissing,
    ShowcaseCase,
    ensure_material,
    list_cases,
    list_cases_with_warnings,
    load_case,
    run_case,
)
from rve.showcases import registry as registry_module

# 合成案例 id（外部素材案例需要真实下载，不在单测里跑）
SYNTHETIC_CASES = ("synth_hard_cut", "synth_dissolve", "synth_fade")

_has_ffmpeg = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(
    not _has_ffmpeg, reason="需要 ffmpeg 合成素材（本机未安装 ffmpeg）"
)


def _write_case(cases_dir: Path, case_id: str, payload: dict) -> Path:
    """在临时 cases 目录里写一份 case.json。"""
    case_dir = cases_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / "case.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _minimal_case(case_id: str, **overrides) -> dict:
    """构造一份最小可用的案例配方（供坏文件/失败校验等场景复用）。"""
    payload = {
        "id": case_id,
        "title": "临时案例",
        "summary": "测试用",
        "material": {
            "kind": "synthetic",
            "license": "CC0-1.0",
            "source": "programmatic",
            "prepare": [
                "ffmpeg -y -v error -f lavfi -i color=c=red:s=320x240:r=30:d=2 "
                "-pix_fmt yuv420p -c:v libx264 {{out}}"
            ],
        },
        "expected": {"shot_count": 1},
        "tags": ["test"],
        "requires_network": False,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #


def test_list_cases_returns_valid_cases() -> None:
    """至少 4 个案例，且每条都能通过 ShowcaseCase 校验。"""
    cases = list_cases()
    assert len(cases) >= 4, f"案例数不足：{[c.id for c in cases]}"
    for case in cases:
        assert isinstance(case, ShowcaseCase)
        # 再校验一次：模型本身必须能吃下自己读出的内容
        assert ShowcaseCase.model_validate(case.model_dump()).id == case.id
        assert case.id
        assert case.title and case.summary
        assert case.material.kind in ("synthetic", "external")

    ids = {case.id for case in cases}
    assert {"synth_hard_cut", "synth_dissolve", "synth_fade", "tos_segment"} <= ids


def test_list_cases_is_sorted_by_id() -> None:
    """列表按 id 排序，便于 CLI/MCP 直接展示。"""
    ids = [case.id for case in list_cases()]
    assert ids == sorted(ids)


def test_load_case_missing_raises_file_not_found() -> None:
    """不存在的 case id 必须抛 FileNotFoundError（不得返回空壳）。"""
    with pytest.raises(FileNotFoundError):
        load_case("不存在的案例-id")


def test_load_case_returns_single_case() -> None:
    """按 id 能读到单个案例，且 id 对得上。"""
    case = load_case("synth_hard_cut")
    assert case.id == "synth_hard_cut"
    assert case.material.kind == "synthetic"
    assert case.expected["shot_count"] == 2


def test_broken_case_json_does_not_break_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """坏 case.json 只让这一条被跳过，整个列表照常可用。"""
    cases_dir = tmp_path / "cases"
    _write_case(cases_dir, "good_case", _minimal_case("good_case"))
    broken_dir = cases_dir / "broken_case"
    broken_dir.mkdir(parents=True, exist_ok=True)
    (broken_dir / "case.json").write_text("{ 这不是合法 JSON ", encoding="utf-8")
    monkeypatch.setattr(registry_module, "_cases_root", lambda: cases_dir)

    cases = list_cases()
    assert [case.id for case in cases] == ["good_case"]

    cases2, warnings = list_cases_with_warnings()
    assert [case.id for case in cases2] == ["good_case"]
    assert warnings, "坏 case.json 必须留下告警"
    assert "broken_case" in warnings[0]


def test_case_with_invalid_field_is_skipped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """字段不符（含未知字段）的 case.json 同样只被跳过。"""
    cases_dir = tmp_path / "cases"
    _write_case(cases_dir, "good_case", _minimal_case("good_case"))
    _write_case(cases_dir, "bad_field", _minimal_case("bad_field", surprise=1))
    monkeypatch.setattr(registry_module, "_cases_root", lambda: cases_dir)

    cases, warnings = list_cases_with_warnings()
    assert [case.id for case in cases] == ["good_case"]
    assert any("bad_field" in w for w in warnings)


# --------------------------------------------------------------------------- #
# 素材
# --------------------------------------------------------------------------- #


def test_external_material_missing_raises_with_guidance(tmp_path: Path) -> None:
    """外部素材缺失：抛 MaterialMissing，且消息里写清"怎么准备"。"""
    case = load_case("tos_segment")
    with pytest.raises(MaterialMissing) as excinfo:
        ensure_material(case, tmp_path)

    message = str(excinfo.value)
    assert case.material.license in message  # 许可
    assert case.material.source in message  # 下载来源
    assert "怎么准备" in message  # 准备指引
    assert "--material" in message  # 放置方式
    assert str(tmp_path / "tos_segment.mp4") in message  # 目标路径


def test_external_material_resolved_from_workdir(tmp_path: Path) -> None:
    """素材已放到工作目录时，直接命中（不联网）。"""
    case = load_case("tos_segment")
    material = tmp_path / "tos_segment.mp4"
    material.write_bytes(b"not-a-real-video")
    assert ensure_material(case, tmp_path) == material


def test_external_material_override_wins(tmp_path: Path) -> None:
    """--material 指定的文件优先于工作目录里的同名文件。"""
    case = load_case("tos_segment")
    override = tmp_path / "custom.mp4"
    override.write_bytes(b"not-a-real-video")
    assert ensure_material(case, tmp_path, str(override)) == override


def test_external_material_override_missing_raises(tmp_path: Path) -> None:
    """--material 指向不存在的文件时也必须显式报错。"""
    case = load_case("tos_segment")
    with pytest.raises(MaterialMissing):
        ensure_material(case, tmp_path, str(tmp_path / "nope.mp4"))


@requires_ffmpeg
def test_synthetic_material_is_generated(tmp_path: Path) -> None:
    """合成素材：ffmpeg 现场生成，产物落在工作目录且可读。"""
    case = load_case("synth_hard_cut")
    material = ensure_material(case, tmp_path)
    assert material == tmp_path / "synth_hard_cut.mp4"
    assert material.is_file()
    assert material.stat().st_size > 0


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case_id", SYNTHETIC_CASES)
@requires_ffmpeg
def test_run_synthetic_case_passes(
    case_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合成案例端到端跑通：ok=True、status=pass、每项校验都通过。"""
    monkeypatch.setenv("RVE_WORKSPACES", str(tmp_path / "workspaces"))
    workdir = tmp_path / "work"

    result = asyncio.run(run_case(case_id, workdir=workdir))

    assert result.ok is True, result.message
    assert result.status == "pass", result.message
    assert result.checks, "必须产出逐项校验结果"
    assert all(check.passed for check in result.checks), [
        (c.name, c.expected, c.actual) for c in result.checks if not c.passed
    ]
    assert result.receipt.ok is True
    # 产物清单：素材 + spec + 导出的 FCP7 XML
    assert len(result.artifacts) == 3
    assert all(Path(a).exists() for a in result.artifacts)


@requires_ffmpeg
def test_run_case_fails_loudly_when_expectation_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """期望值对不上必须判 fail —— 绝不 quietly succeed。"""
    monkeypatch.setenv("RVE_WORKSPACES", str(tmp_path / "workspaces"))
    cases_dir = tmp_path / "cases"
    payload = _minimal_case(
        "mismatch_case",
        expected={"shot_count": 99},  # 故意写错
    )
    _write_case(cases_dir, "mismatch_case", payload)
    monkeypatch.setattr(registry_module, "_cases_root", lambda: cases_dir)

    result = asyncio.run(run_case("mismatch_case", workdir=tmp_path / "work"))

    assert result.ok is False
    assert result.status == "fail"
    assert result.message
    failed = [c for c in result.checks if not c.passed]
    assert failed and failed[0].name == "shot_count"
    assert failed[0].expected == "99"
    assert failed[0].actual == "1"


def test_run_case_reports_missing_material(tmp_path: Path) -> None:
    """外部素材缺失：返回 status=material_missing，不抛异常穿透。"""
    result = asyncio.run(run_case("tos_segment", workdir=tmp_path / "work"))
    assert result.ok is False
    assert result.status == "material_missing"
    assert "怎么准备" in result.message
    assert result.receipt.ok is False


def test_run_case_unknown_id_raises(tmp_path: Path) -> None:
    """不存在的案例 id 直接抛 FileNotFoundError（不吞异常）。"""
    with pytest.raises(FileNotFoundError):
        asyncio.run(run_case("不存在的案例-id", workdir=tmp_path / "work"))
