"""转场识别（transitions）测试。

素材在测试时用 ffmpeg 现合成：一段"硬切 → 溶解 → 淡出到黑"的拼片。
判据只看**分类器标签**（不追求与 PySceneDetect 边界完全一致：渐进过渡上
PySceneDetect 的切点本就可能落在渐变中段）。

覆盖：
- 硬切 / 溶解 / 淡出到黑 三类标签正确；
- 证据不足（边界落在静止片段中）→ 进 uncertain[]，而不是静默猜一个标签；
- 经 build_edit_spec 落盘后，cut_type / transition 一致且不破坏时间轴不变量。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from rve.pipeline.build import build_edit_spec, get_edit_spec
from rve.pipeline.detect import detect_shots
from rve.pipeline.probe import probe_video
from rve.pipeline.transitions import (
    classify_window,
    detect_transitions,
    frame_stats,
    sample_window,
)
from rve.schema.editspec import AudioInfo
from rve.schema.enums import CutType
from rve.schema.results import AudioAnalysisResult, ToolReceipt

W, H, FPS = 64, 48, 24

# 合成素材的时间结构（秒）：
#   red 0.0-2.5 | hard cut | green 2.5-3.5 | dissolve(1s) 3.5-4.5 | blue 4.5-5.0
#   | fadeblack(1s) 5.0-6.0 | white 6.0-7.5
HARD_AT = 2.5          # 硬切红->绿
DISSOLVE_AT = 4.0      # 溶解中点（绿->蓝）
FADE_AT = 5.5          # 淡出到黑的中点（蓝->黑->白）
NOTHING_AT = 1.0       # 落在纯红静止段内：证据不足

_has_ffmpeg = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(not _has_ffmpeg, reason="需要 ffmpeg 合成/取帧")


def _build_mix(dst: Path) -> None:
    """合成 硬切 / 溶解 / 淡出到黑 三段拼接的测试素材。"""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=red:s={W}x{H}:r={FPS}:d=2.5",
        "-f", "lavfi", "-i", f"color=c=green:s={W}x{H}:r={FPS}:d=2.5",
        "-f", "lavfi", "-i", f"color=c=blue:s={W}x{H}:r={FPS}:d=2.5",
        "-f", "lavfi", "-i", f"color=c=white:s={W}x{H}:r={FPS}:d=2.5",
        "-filter_complex",
        "[0:v]fps=24,settb=1/24,format=yuv420p[a0];"
        "[1:v]fps=24,settb=1/24,format=yuv420p[a1];"
        "[2:v]fps=24,settb=1/24,format=yuv420p[a2];"
        "[3:v]fps=24,settb=1/24,format=yuv420p[a3];"
        "[a0][a1]concat=n=2:v=1:a=0[ab];"
        "[ab]settb=1/24[ab2];"
        "[ab2][a2]xfade=transition=dissolve:duration=1:offset=3.5[v1];"
        "[v1][a3]xfade=transition=fadeblack:duration=1:offset=5[v2]",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-map", "[v2]", str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def mix(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """模块级共享的测试素材（合成一次）。"""
    if not _has_ffmpeg:
        pytest.skip("需要 ffmpeg 合成素材")
    dst = tmp_path_factory.mktemp("transitions") / "mix.mp4"
    _build_mix(dst)
    return dst


def _verdicts(mix: Path):
    probe = asyncio.run(probe_video(str(mix)))
    boundaries = [
        (1, HARD_AT),
        (2, DISSOLVE_AT),
        (3, FADE_AT),
        (4, NOTHING_AT),
    ]
    result = detect_transitions(
        str(mix), boundaries, probe.source.fps, probe.source.duration_sec
    )
    return {v.index: v for v in result.verdicts}


@requires_ffmpeg
def test_labels_hard_dissolve_fade(mix: Path) -> None:
    """三类转场标签必须与合成素材的时间结构一致。"""
    v = _verdicts(mix)

    assert v[1].cut_type == CutType.HARD, v[1].evidence
    assert v[1].uncertain is False

    assert v[2].cut_type == CutType.DISSOLVE, v[2].evidence
    assert v[2].uncertain is False
    # 溶解时长 ~1.0s（±容差）
    assert 0.4 <= v[2].duration_sec <= 1.5, v[2].duration_sec

    assert v[3].cut_type == CutType.FADE, v[3].evidence
    assert v[3].uncertain is False
    assert 0.4 <= v[3].duration_sec <= 1.5, v[3].duration_sec

    # 证据链：每条判定都带可复核的统计量
    for verdict in v.values():
        assert "peak_diff" in verdict.evidence
        assert "baseline_diff" in verdict.evidence
        assert "active_frames" in verdict.evidence


@requires_ffmpeg
def test_insufficient_evidence_goes_uncertain(mix: Path) -> None:
    """边界落在静止片段中：证据不足必须进 uncertain，而不是静默猜一个标签。"""
    v = _verdicts(mix)
    nothing = v[4]

    assert nothing.cut_type == CutType.UNKNOWN
    assert nothing.uncertain is True
    assert nothing.reason, "存疑必须给出理由"
    # 绝不静默猜测：不能是硬切/溶解/淡出
    assert nothing.cut_type not in (CutType.HARD, CutType.DISSOLVE, CutType.FADE)


def test_classify_window_is_pure_and_flags_missing_evidence() -> None:
    """纯函数判定：平坦（无变化）序列必须判为证据不足，而非默认硬切。"""
    lum = [0.5] * 20
    diffs = [0.0] * 19
    verdict = classify_window(lum, diffs, FPS)
    assert verdict.cut_type == CutType.UNKNOWN
    assert verdict.uncertain is True

    # 采样过短同样算证据不足
    short = classify_window([0.5, 0.5], [0.0], FPS)
    assert short.cut_type == CutType.UNKNOWN
    assert short.uncertain is True


def test_classify_window_synthetic_curves() -> None:
    """用构造曲线直测判据：单帧大阶跃=硬切；多帧缓变=溶解；亮度塌陷=淡出。"""
    n = 24
    # 硬切：其余帧全同，仅一处 0.5 阶跃
    diffs = [0.0] * (n - 1)
    diffs[11] = 0.5
    hard = classify_window([0.4] * n, diffs, FPS)
    assert hard.cut_type == CutType.HARD

    # 溶解：静止帧 + 中间连续多帧小阶跃（贴合真实窗口：两端静止、中段渐变）
    dissolve_diffs = [0.0] * 6 + [0.028] * 10 + [0.0] * 7
    dissolve = classify_window([0.4] * n, dissolve_diffs, FPS)
    assert dissolve.cut_type == CutType.DISSOLVE

    # 淡出到黑：两端明亮、中段亮度平滑塌到近黑
    ramp = [0.6 - 0.6 * (k / 9.0) for k in range(10)]
    lum = [0.6] * 6 + ramp + [0.0] * 8
    fade = classify_window(lum, [0.03] * (len(lum) - 1), FPS)
    assert fade.cut_type == CutType.FADE


def test_low_contrast_hard_cut_keeps_confidence() -> None:
    """低对比硬切：绝对阶跃不大，但远高于窗口自身噪声 → 应判硬切且不存疑。

    真实素材（Tears of Steel 片段）9 条边界里有 4 条硬切的绝对阶跃仅 0.14~0.21、
    低于绝对参考值 0.35，信噪比却达 50~150 倍。若只看绝对值会把它们误标为 uncertain。
    """
    n = 24
    noise, step = 0.002, 0.15
    diffs = [noise] * (n - 1)
    diffs[11] = step
    verdict = classify_window([0.4] * n, diffs, FPS)

    assert verdict.cut_type == CutType.HARD, verdict.evidence
    assert verdict.uncertain is False, verdict.reason
    assert verdict.confidence >= 0.5
    assert verdict.evidence["contrast_ratio"] == pytest.approx(step / noise, rel=0.1)


def test_sub_floor_step_is_not_claimed_as_hard_cut() -> None:
    """低于绝对下限的单帧阶跃：即便信噪比很高，也不许认成硬切（宁可标 uncertain）。

    红线：相对判据只用于**置信度**，不得放宽分支条件。否则弱阶跃/噪声尖峰会被
    凭空升格为"确定的硬切"。
    """
    n = 24
    diffs = [0.002] * (n - 1)
    diffs[11] = 0.08  # 高于 active 阈值（0.015）但低于 HARD_STEP_MIN（0.12）
    verdict = classify_window([0.4] * n, diffs, FPS)

    assert verdict.cut_type == CutType.UNKNOWN, verdict.evidence
    assert verdict.uncertain is True
    assert verdict.cut_type != CutType.HARD


@requires_ffmpeg
def test_build_writes_transitions_and_keeps_invariants(
    mix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """经 build 落盘后：cut_type / transition 自洽，且时间轴不变量未被破坏。"""
    monkeypatch.setenv("RVE_WORKSPACES", str(tmp_path))
    probe = asyncio.run(probe_video(str(mix)))
    det = asyncio.run(detect_shots(str(mix)))
    audio = AudioAnalysisResult(audio=AudioInfo(), receipt=ToolReceipt(tool="stub"))

    built = asyncio.run(build_edit_spec(probe, det, audio, spec_id="transitions-test"))
    assert built.receipt.ok

    spec = asyncio.run(get_edit_spec("transitions-test"))
    assert spec.shots

    # 时间轴连续、铺满（EditSpec 构造时已强校验，这里复核落盘结果）
    ordered = sorted(spec.shots, key=lambda s: s.in_sec)
    for prev, cur in zip(ordered, ordered[1:]):
        assert cur.in_sec == pytest.approx(prev.out_sec, abs=1e-6)
    assert ordered[-1].out_sec == pytest.approx(spec.source.duration_sec, abs=0.05)

    for shot in spec.shots:
        assert isinstance(shot.cut_type, CutType)
        if shot.transition is not None:
            # 渐变过渡：类型一致且时长为正
            assert shot.cut_type == shot.transition.type
            assert shot.transition.duration_sec > 0
        # 硬切不应带 transition
        if shot.cut_type == CutType.HARD:
            assert shot.transition is None

    # 找不到转场的判定必须以 uncertain[] 显式暴露，绝不静默
    for item in spec.uncertain:
        assert item.field_path and item.reason

    # 回执落盘：兑现"每个 spec 目录含 spec.json 与 receipts/"，且转场**逐条**证据
    # 在交付物层面可复核（不依赖 MCP 的瞬时返回，避免"证据只存在于内存"）。
    receipt_dir = tmp_path / "transitions-test" / "receipts"
    build_receipt = receipt_dir / "build_edit_spec.json"
    transitions_receipt = receipt_dir / "transitions.json"
    assert build_receipt.is_file(), "receipts/build_edit_spec.json 未落盘"
    assert transitions_receipt.is_file(), "receipts/transitions.json 未落盘"

    payload = json.loads(transitions_receipt.read_text(encoding="utf-8"))
    verdicts = payload["params"]["verdicts"]
    assert len(verdicts) == len(spec.shots) - 1  # 每条内部边界一条
    for record in verdicts:
        for key in ("cut_type", "confidence", "peak_diff", "baseline_diff", "window_sec"):
            assert key in record, f"边界证据缺 {key}: {record}"
        assert record["cut_type"] in {t.value for t in CutType}

    # build 回执的产物清单必须包含 spec.json 与两份 receipts
    artifacts = {Path(a).name for a in json.loads(build_receipt.read_text("utf-8"))["artifacts"]}
    assert {"spec.json", "build_edit_spec.json", "transitions.json"} <= artifacts


@requires_ffmpeg
def test_sampling_only_touches_boundary_window(mix: Path) -> None:
    """取帧只解边界附近的窗口：请求 0.1s 窗口就只应回 ~0.1s×fps 帧。"""
    frames = sample_window(str(mix), 1.0, 0.1)
    assert len(frames) == pytest.approx(0.1 * FPS, abs=2)
    lum, diffs = frame_stats(frames)
    assert len(lum) == len(frames)
    assert len(diffs) == len(frames) - 1
    # 纯红静止段：帧间几乎无差异
    assert max(diffs) <= 0.01
