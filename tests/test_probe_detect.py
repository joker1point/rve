"""probe / detect 的端到端测试（依赖 ffprobe 与 scenedetect，缺失则跳过）。"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from rve.pipeline.build import build_edit_spec, get_edit_spec
from rve.pipeline.detect import detect_shots
from rve.pipeline.probe import probe_video
from rve.schema.editspec import AudioInfo
from rve.schema.enums import DetectEngine
from rve.schema.results import AudioAnalysisResult, ToolReceipt

DATA_DIR = Path(__file__).parent / "data"
SYNTH = DATA_DIR / "synth.mp4"
SYNTH_CUT = DATA_DIR / "synth_cut.mp4"

# 时长/连续性容差
DUR_TOL = 0.3
ADJ_TOL = 1e-3
TAIL_TOL = 0.05

_has_ffprobe = shutil.which("ffprobe") is not None
try:  # scenedetect 是否可用
    import scenedetect  # noqa: F401

    _has_scenedetect = True
except ImportError:  # pragma: no cover
    _has_scenedetect = False

requires_probe = pytest.mark.skipif(
    not _has_ffprobe or not SYNTH.exists(),
    reason="需要 ffprobe 与 tests/data/synth.mp4",
)
requires_detect = pytest.mark.skipif(
    not (_has_ffprobe and _has_scenedetect and SYNTH_CUT.exists()),
    reason="需要 ffprobe、scenedetect 与 tests/data/synth_cut.mp4",
)


def _run(coro):
    return asyncio.run(coro)


@requires_probe
def test_probe_synth_metadata() -> None:
    result = _run(probe_video(str(SYNTH)))
    src = result.source
    assert src.fps == pytest.approx(30.0, abs=0.1)
    assert src.width == 320 and src.height == 240
    assert src.duration_sec == pytest.approx(6.0, abs=DUR_TOL)
    assert src.has_audio is True
    assert src.video_codec == "h264"
    assert result.receipt.ok is True
    assert result.receipt.duration_ms >= 0


@requires_detect
def test_detect_shots_contiguous_and_tiled() -> None:
    probe = _run(probe_video(str(SYNTH_CUT)))
    det = _run(detect_shots(str(SYNTH_CUT)))

    assert det.shots, "检测结果不应为空"
    assert len(det.shots) >= 2, "硬切拼接片应至少检测出 2 个镜头"
    assert isinstance(det.disagreements, list)

    ordered = sorted(det.shots, key=lambda s: s.in_sec)
    for prev, cur in zip(ordered, ordered[1:]):
        assert cur.in_sec == pytest.approx(prev.out_sec, abs=ADJ_TOL)

    # 首镜起点为 0；末镜终点约等于源时长
    assert ordered[0].in_sec == pytest.approx(0.0, abs=ADJ_TOL)
    assert ordered[-1].out_sec == pytest.approx(
        probe.source.duration_sec, abs=TAIL_TOL
    )


@requires_detect
def test_detect_single_shot_when_no_cut() -> None:
    det = _run(detect_shots(str(SYNTH), engine=DetectEngine.CONTENT))
    # testsrc2 无硬切：应得到单镜头且铺满全片
    assert len(det.shots) == 1
    assert det.shots[0].in_sec == pytest.approx(0.0, abs=ADJ_TOL)
    assert det.disagreements == []


@requires_detect
def test_detect_auto_records_disagreements() -> None:
    det = _run(detect_shots(str(SYNTH_CUT), engine=DetectEngine.AUTO))
    assert isinstance(det.disagreements, list)
    assert det.engine_used.endswith("content+adaptive")


@requires_detect
def test_detect_transnet_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        _run(detect_shots(str(SYNTH_CUT), engine=DetectEngine.TRANSNET))


@requires_detect
def test_build_and_reload_spec(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RVE_WORKSPACES", str(tmp_path))
    probe = _run(probe_video(str(SYNTH_CUT)))
    det = _run(detect_shots(str(SYNTH_CUT)))
    audio = AudioAnalysisResult(
        audio=AudioInfo(), receipt=ToolReceipt(tool="stub")
    )

    built = _run(build_edit_spec(probe, det, audio, spec_id="build-test"))
    assert built.spec_id == "build-test"
    spec = _run(get_edit_spec("build-test"))

    # EditSpec 自身的不变量（连续 + 铺满）已由契约校验保证
    assert spec.shots
    assert spec.shots[-1].out_sec == pytest.approx(
        spec.source.duration_sec, abs=TAIL_TOL
    )
    assert spec.rhythm.shot_count == len(spec.shots)
    assert spec.rhythm.cuts_per_min > 0

    # 回执落盘：兑现 workspace 的承诺——每个 spec 目录含 spec.json 与 receipts/
    assert (tmp_path / "build-test" / "receipts" / "build_edit_spec.json").is_file()
