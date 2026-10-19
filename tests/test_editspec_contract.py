"""EditSpec 契约红线测试 —— 锁死 Wave 0 冻结点。

这 7 条是并发协作与导出正确性的地基，任何一条变红都说明契约被破坏。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rve.schema.editspec import EditSpec, Shot, VideoSource
from rve.schema.enums import CutType

FIXTURE = Path(__file__).parent / "fixtures" / "sample_editspec.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _source() -> VideoSource:
    return VideoSource(path="x.mp4", duration_sec=10.0, fps=30.0, width=1920, height=1080)


# 红线 1：秒是唯一时间单位——结构里不得出现时间码字段
def test_no_timecode_fields_in_schema() -> None:
    banned = ("_tc", "timecode", "_frames", "frames_")
    for name in EditSpec.model_fields:
        assert not name.endswith(banned), f"EditSpec 出现时间码字段: {name}"
    for name in Shot.model_fields:
        assert not name.endswith(banned), f"Shot 出现时间码字段: {name}"
    assert "in_sec" in Shot.model_fields and "out_sec" in Shot.model_fields


# 红线 2：夹具是合法 EditSpec（镜头连续 + 铺满时间轴）
def test_fixture_is_valid_spec() -> None:
    spec = EditSpec.model_validate(_load())
    assert spec.shots, "夹具必须含镜头"
    assert spec.shots[-1].out_sec == pytest.approx(spec.source.duration_sec, abs=1e-2)


# 红线 2b：出现空隙必须抛错
def test_gap_between_shots_rejected() -> None:
    data = _load()
    data["shots"][2]["in_sec"] = 5.5  # 故意制造空隙: 5.02 -> 5.5
    with pytest.raises(ValueError):
        EditSpec.model_validate(data)


# 红线 2c：未铺满时间轴必须抛错
def test_not_covering_timeline_rejected() -> None:
    data = _load()
    data["shots"] = data["shots"][:3]  # 只到 6.0s，源时长 12.34s
    with pytest.raises(ValueError):
        EditSpec.model_validate(data)


# 红线 3 + 4：未知即显式；未声明字段一律拒绝（防接口漂移）
def test_unknown_cut_type_rejected() -> None:
    data = _load()
    data["shots"][0]["cut_type"] = "隐形转场"
    with pytest.raises(ValueError):
        EditSpec.model_validate(data)
    assert CutType.UNKNOWN == "unknown"


def test_extra_field_rejected() -> None:
    data = _load()
    data["shots"][0]["my_secret_field"] = 1
    with pytest.raises(ValueError):
        EditSpec.model_validate(data)


# 红线 5：存疑必须显式登记，不得静默
def test_uncertain_exposed() -> None:
    spec = EditSpec.model_validate(_load())
    assert spec.uncertain, "夹具应含存疑项"
    assert all(u.field_path and u.reason for u in spec.uncertain)

    spec.mark_uncertain("shots[0].shot_size", "景别置信度低", ["close_up", "medium"])
    assert spec.uncertain[-1].field_path == "shots[0].shot_size"


# 红线 6：round-trip 稳定（schema 可序列化并可再校验）
def test_roundtrip() -> None:
    spec = EditSpec.model_validate(_load())
    again = EditSpec.model_validate_json(spec.model_dump_json())
    assert again.model_dump() == spec.model_dump()


# 红线 7：渐变类过渡必须带时长，硬切不带
def test_transition_consistency() -> None:
    spec = EditSpec.model_validate(_load())
    for shot in spec.shots:
        if shot.transition is not None:
            assert shot.transition.duration_sec > 0, "渐变过渡必须有正时长"
            assert shot.cut_type == shot.transition.type


# 附加：单镜头时长为零或负必须拒绝
def test_non_positive_shot_rejected() -> None:
    with pytest.raises(ValueError):
        Shot(index=0, in_sec=1.0, out_sec=1.0)


# 附加：单镜头必须铺满整条时间轴
def test_single_shot_tiling() -> None:
    spec = EditSpec(
        spec_id="s1",
        source=_source(),
        shots=[Shot(index=0, in_sec=0.0, out_sec=10.0)],
    )
    assert len(spec.shots) == 1
