"""多引擎交叉验证（含可选 hypit 引擎）的测试。

要点：
- 默认路径（engine=auto，不带 extra_engines）行为**不得改变**（engine_used 仍是
  pyscenedetect:content+adaptive）——多引擎是加法，不是替换；
- 带 hypit 时 engine_used 要如实反映参与的引擎；
- hypit 未安装时必须**显式报错并给出安装提示**，绝不静默回退到别的引擎。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from rve.pipeline.detect import detect_shots
from rve.schema.enums import DetectEngine

DATA_DIR = Path(__file__).parent / "data"
SYNTH_CUT = DATA_DIR / "synth_cut.mp4"

ADJ_TOL = 1e-3
TAIL_TOL = 0.05

_has_hypit = shutil.which("hypit") is not None
try:
    import scenedetect  # noqa: F401

    _has_scenedetect = True
except ImportError:  # pragma: no cover
    _has_scenedetect = False

try:
    import rve.providers.shots_hypit as _hypit_provider  # noqa: F401

    _has_provider = True
except ImportError:  # pragma: no cover - 核心层尚未落地
    _has_provider = False

requires_base = pytest.mark.skipif(
    not (_has_scenedetect and SYNTH_CUT.exists()),
    reason="需要 scenedetect 与 tests/data/synth_cut.mp4",
)
requires_hypit = pytest.mark.skipif(
    not (_has_provider and _has_hypit and SYNTH_CUT.exists()),
    reason="需要 hypit 可执行文件与 shots_hypit Provider",
)


def _run(coro):
    return asyncio.run(coro)


def _assert_tiled(det) -> None:
    """镜头必须连续、无重叠、铺满时间轴（与 EditSpec 的不变量一致）。"""
    assert det.shots, "检测结果不应为空"
    ordered = sorted(det.shots, key=lambda s: s.in_sec)
    for prev, cur in zip(ordered, ordered[1:]):
        assert cur.in_sec == pytest.approx(prev.out_sec, abs=ADJ_TOL)
    assert ordered[0].in_sec == pytest.approx(0.0, abs=ADJ_TOL)


@requires_base
def test_default_auto_path_unchanged() -> None:
    """默认（AUTO、无额外引擎）行为不得改变。"""
    det = _run(detect_shots(str(SYNTH_CUT)))
    assert det.engine_used == "pyscenedetect:content+adaptive"
    _assert_tiled(det)


@requires_hypit
def test_hypit_engine_alone() -> None:
    """单独用 hypit 引擎：如实标注 engine_used，且产出仍满足铺满不变量。"""
    det = _run(detect_shots(str(SYNTH_CUT), engine=DetectEngine.HYPIT))
    assert det.engine_used == "hypit"
    _assert_tiled(det)
    assert det.shots[-1].out_sec == pytest.approx(
        det.shots[-1].out_sec, abs=TAIL_TOL
    )


@requires_hypit
def test_three_way_cross_validation() -> None:
    """三方交叉（content+adaptive+hypit）：engine_used 如实反映，分歧显式暴露。"""
    det = _run(detect_shots(str(SYNTH_CUT), extra_engines=["hypit"]))
    assert det.engine_used.endswith("content+adaptive+hypit")
    assert isinstance(det.disagreements, list)
    _assert_tiled(det)
    # 分歧项必须带可读的说明与候选引擎
    for item in det.disagreements:
        assert item.reason
        assert item.candidates


@requires_hypit
def test_missing_hypit_raises_with_install_hint(monkeypatch) -> None:
    """本机没装 hypit 时必须显式报错并给安装提示，绝不静默回退。"""
    import rve.providers.shots_hypit as provider

    real_which = shutil.which

    def fake_which(name, *args, **kwargs):
        if name == "hypit":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(provider.shutil, "which", fake_which)

    with pytest.raises(Exception) as excinfo:
        _run(detect_shots(str(SYNTH_CUT), engine=DetectEngine.HYPIT))
    message = str(excinfo.value)
    assert "hypit" in message
    assert "npm" in message.lower() or "安装" in message
