"""hypit 镜头检测 Provider 的测试。

分两类：
- 纯函数单测：聚类、筛选规则、JSON 解析——**不真正调用 hypit**；
- 集成用例：需要本机装了 hypit（用 shutil.which 判定），否则 skip。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from rve.providers.shots_hypit import (
    MIN_SCORE,
    HypitBoundaryDetector,
    HypitCandidate,
    HypitCluster,
    _cluster_candidates,
    _parse_boundaries,
    _parse_probe,
    _select_hard_cuts,
)
from rve.schema.enums import DetectEngine

DATA_DIR = Path(__file__).parent / "data"
SYNTH_CUT = DATA_DIR / "synth_cut.mp4"

# DetectEngine.HYPIT 由主线程新增；枚举尚未落地时相关用例 skip
HYPIT_ENGINE = getattr(DetectEngine, "HYPIT", None)

requires_hypit = pytest.mark.skipif(
    shutil.which("hypit") is None, reason="需要本机安装 hypit（npm i -g @hypit/hypit）"
)
requires_hypit_engine = pytest.mark.skipif(
    HYPIT_ENGINE is None, reason="DetectEngine.HYPIT 尚未加入枚举（主线程负责）"
)


def _cands(pairs: list[tuple[float, float]]) -> list[HypitCandidate]:
    return [HypitCandidate(sec=sec, score=score) for sec, score in pairs]


# --- 聚类 -----------------------------------------------------------------


def test_cluster_merges_close_candidates() -> None:
    """间隔 0.05s 的 3 个候选 → 1 簇，代表切点取簇内最高分候选。"""
    clusters = _cluster_candidates(
        _cands([(1.00, 0.30), (1.05, 0.40), (1.10, 0.35)])
    )
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.member_count == 3
    assert cluster.score == pytest.approx(0.40)
    assert cluster.sec == pytest.approx(1.05)
    assert [m.sec for m in cluster.members] == pytest.approx([1.00, 1.05, 1.10])


def test_cluster_splits_distant_candidates() -> None:
    """间隔 0.5s 的 2 个候选 → 2 簇。"""
    clusters = _cluster_candidates(_cands([(1.00, 0.40), (1.50, 0.45)]))
    assert len(clusters) == 2
    assert [c.member_count for c in clusters] == [1, 1]
    assert [c.sec for c in clusters] == pytest.approx([1.00, 1.50])


def test_cluster_chain_merges_gradual_region() -> None:
    """渐变区的一串密集候选（0.083s 间隔）→ 并成 1 簇（与实测 hypit 输出一致）。"""
    dense = _cands(
        [
            (5.25, 0.129),
            (5.333, 0.200),
            (5.417, 0.133),
            (5.50, 0.200),
            (5.583, 0.135),
            (5.667, 0.196),
            (5.833, 0.133),
            (6.00, 0.132),
            (6.167, 0.128),
        ]
    )
    clusters = _cluster_candidates(dense)
    assert len(clusters) == 1
    assert clusters[0].member_count == 9
    assert clusters[0].score == pytest.approx(0.200)
    # 代表切点 = 簇内最高分候选（并列时取最靠前的一个）
    assert clusters[0].sec == pytest.approx(5.333)


# --- 筛选规则 --------------------------------------------------------------


def test_select_hard_cuts_applies_density_and_score_rules() -> None:
    """规则 A（密集簇）与规则 B（低分单候选）都被排除，高分单候选被采用。"""
    dense = HypitCluster(sec=5.25, score=0.20, member_count=9)
    low = HypitCluster(sec=3.0, score=0.10, member_count=1)
    good = HypitCluster(sec=2.0, score=0.466, member_count=1)

    kept, warnings = _select_hard_cuts([dense, low, good])

    assert [c.sec for c in kept] == pytest.approx([2.0])
    # 被排除的簇必须显式出现在告警里，不得静默丢弃
    assert len(warnings) == 2
    joined = "\n".join(warnings)
    assert "9" in joined and "0.20" in joined
    assert "0.10" in joined
    assert "未作为硬切切点" in joined


def test_select_hard_cuts_keeps_high_score_single_candidate() -> None:
    """member_count == 1 且分数 ≥ MIN_SCORE 的簇被采用。"""
    cluster = HypitCluster(sec=3.5, score=0.378, member_count=1)
    kept, warnings = _select_hard_cuts([cluster])
    assert kept == [cluster]
    assert warnings == []


def test_min_score_threshold_is_used() -> None:
    """分数恰好等于 MIN_SCORE 时应被采用（阈值为闭区间下界）。"""
    cluster = HypitCluster(sec=1.0, score=MIN_SCORE, member_count=1)
    kept, warnings = _select_hard_cuts([cluster])
    assert kept == [cluster]
    assert warnings == []


# --- JSON 解析 -------------------------------------------------------------


def test_parse_boundaries_reads_candidates() -> None:
    """解析 hypit media boundaries --json 的结构（不真正调用 hypit）。"""
    payload = {
        "path": "x.mp4",
        "sampleRate": 12,
        "threshold": 0.1,
        "candidates": [
            {"at": 2, "score": 0.466},
            {"at": 3.5, "score": 0.378},
        ],
    }
    candidates, sample_rate, threshold, warnings = _parse_boundaries(payload)
    assert [(c.sec, c.score) for c in candidates] == pytest.approx(
        [(2.0, 0.466), (3.5, 0.378)]
    )
    assert sample_rate == pytest.approx(12.0)
    assert threshold == pytest.approx(0.1)
    assert warnings == []


def test_parse_boundaries_sorts_candidates() -> None:
    """候选按 at 升序返回。"""
    payload = {
        "sampleRate": 12,
        "threshold": 0.1,
        "candidates": [{"at": 3.5, "score": 0.3}, {"at": 2.0, "score": 0.4}],
    }
    candidates, _, _, _ = _parse_boundaries(payload)
    assert [c.sec for c in candidates] == pytest.approx([2.0, 3.5])


def test_parse_boundaries_rejects_malformed_candidate() -> None:
    """候选字段缺失时抛 RuntimeError（绝不 quietly succeed）。"""
    payload = {"sampleRate": 12, "threshold": 0.1, "candidates": [{"at": 2.0}]}
    with pytest.raises(RuntimeError):
        _parse_boundaries(payload)


def test_parse_probe_reads_duration_and_fps() -> None:
    """解析 hypit media probe --json 的实测字段名 duration / frameRate。"""
    duration, fps, warnings = _parse_probe(
        {
            "path": "x.mp4",
            "duration": 6.024,
            "hasVideo": True,
            "width": 320,
            "height": 240,
            "frameRate": 30,
            "hasAudio": True,
        }
    )
    assert duration == pytest.approx(6.024)
    assert fps == pytest.approx(30.0)
    assert warnings == []


def test_parse_probe_requires_duration() -> None:
    """取不到时长是硬错误（无法铺满时间轴）。"""
    with pytest.raises(RuntimeError):
        _parse_probe({"path": "x.mp4", "hasVideo": True})


def test_parse_probe_warns_when_fps_missing() -> None:
    """取不到帧率时给保守占位并记 warning，不静默吞掉。"""
    duration, fps, warnings = _parse_probe({"duration": 4.0})
    assert duration == pytest.approx(4.0)
    assert fps == pytest.approx(0.0)
    assert warnings and "frameRate" in warnings[0]


# --- 缺少 hypit ------------------------------------------------------------


def test_missing_hypit_raises_runtime_error(monkeypatch) -> None:
    """找不到 hypit 时抛 RuntimeError，且消息含安装提示（可选依赖）。"""
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError) as excinfo:
        HypitBoundaryDetector().run_boundaries(str(SYNTH_CUT))
    message = str(excinfo.value)
    assert "npm i -g @hypit/hypit" in message
    assert "可选" in message


def test_detect_rejects_other_engines() -> None:
    """本 Provider 只实现 engine=hypit，其余引擎抛 NotImplementedError。"""
    with pytest.raises(NotImplementedError):
        HypitBoundaryDetector().detect(
            str(SYNTH_CUT), engine=DetectEngine.CONTENT
        )


def test_boundaries_failure_raises_runtime_error(monkeypatch) -> None:
    """hypit 非 0 退出码 → 抛 RuntimeError，并带上错误信息。

    实测：hypit 出错时 stderr 为空，错误以 JSON 信封写在 stdout。
    """

    class _Proc:
        def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **_kwargs):
        if "probe" in cmd:
            return _Proc(0, json.dumps({"duration": 6.0, "frameRate": 30}))
        return _Proc(
            1,
            json.dumps(
                {
                    "format": "hypit.cli-error@1",
                    "ok": False,
                    "error": {"code": "CLI_ERROR", "message": "cannot read x.mp4"},
                }
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as excinfo:
        HypitBoundaryDetector().run_boundaries("x.mp4")
    assert "cannot read x.mp4" in str(excinfo.value)


def test_boundaries_invalid_json_raises_runtime_error(monkeypatch) -> None:
    """hypit 输出非合法 JSON → 抛 RuntimeError（绝不 quietly succeed）。"""

    class _Proc:
        def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **_kwargs):
        if "probe" in cmd:
            return _Proc(0, json.dumps({"duration": 6.0, "frameRate": 30}))
        return _Proc(0, "not-json-at-all")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        HypitBoundaryDetector().run_boundaries("x.mp4")


# --- 集成用例 ---------------------------------------------------------------


@requires_hypit
def test_run_boundaries_on_synth_cut() -> None:
    """真跑 hypit：候选落在 [0, 时长] 内，且至少聚出 1 个簇。"""
    run = HypitBoundaryDetector().run_boundaries(str(SYNTH_CUT))

    assert run.duration_sec > 0
    assert run.fps > 0
    assert run.sample_rate > 0
    assert run.clusters, "合成硬切片应至少聚出 1 个候选簇"
    for cand in run.raw:
        assert 0.0 <= cand.sec <= run.duration_sec + 1e-6
    for cluster in run.clusters:
        assert cluster.member_count == len(cluster.members) >= 1
        assert cluster.score == pytest.approx(
            max(m.score for m in cluster.members)
        )


@requires_hypit
@requires_hypit_engine
def test_detect_tiles_timeline() -> None:
    """detect：镜头连续、无重叠、铺满时间轴；engine_used 写明实际引擎。"""
    result = HypitBoundaryDetector().detect(str(SYNTH_CUT), engine=HYPIT_ENGINE)

    assert result.engine_used == "hypit:boundaries"
    assert result.shots
    assert result.disagreements == []
    ordered = sorted(result.shots, key=lambda s: s.in_sec)
    assert ordered[0].in_sec == pytest.approx(0.0, abs=1e-3)
    for prev, cur in zip(ordered, ordered[1:]):
        assert cur.in_sec == pytest.approx(prev.out_sec, abs=1e-3)
    assert ordered[-1].out_sec == pytest.approx(
        HypitBoundaryDetector().run_boundaries(str(SYNTH_CUT)).duration_sec, abs=0.05
    )
    for shot in result.shots:
        assert shot.cut_type.value == "hard"
        assert 0.0 <= shot.confidence.boundary <= 1.0


@requires_hypit
@requires_hypit_engine
def test_detect_with_out_of_range_threshold_warns_instead_of_failing() -> None:
    """共享的 threshold 若是 PySceneDetect 量纲（如 27），不应让 hypit 调用直接失败。

    hypit 的 --threshold 只接受 0~1（实测越界会以 rc=1 退出），因此 Provider
    改用 hypit 默认阈值，并把这件事写进 receipt.warnings。
    """
    result = HypitBoundaryDetector().detect(
        str(SYNTH_CUT), engine=HYPIT_ENGINE, threshold=27.0
    )
    assert result.shots
    assert any("threshold" in w for w in result.receipt.warnings)
