"""镜头检测步骤：把多引擎检测异步化。

多引擎交叉验证的语义（不随引擎数量改变）
--------------------------------------
- 各引擎的切点按 ±MATCH_TOL 聚成簇；
- 簇内按采信优先级（content > adaptive > hypit）取代表切点，分数取簇内最高分；
- 簇未被**所有**参与引擎命中时，写入 disagreements —— 绝不静默取其一。

可选引擎：hypit
--------------
hypit 是本机 npm 全局包（@hypit/hypit），提供「机械的相邻帧变化候选 + 强度
分数」。它**不是**剪辑级镜头标签，剪辑级转场语义仍由 pipeline/transitions.py
判定。未安装 hypit 时相关调用会显式报错（带安装提示），绝不静默回退到别的引擎。

交叉验证放在这一层（而非某个 Provider 内部），因为"N 个引擎如何互证"是编排
问题：每个 Provider 只负责产出自己那一路切点。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from ..providers.shots_pyscenedetect import (
    MATCH_TOL,
    _Boundary,
    _build_shots,
    _DetectorRun,
    run_pyscenedetect,
)
from ..schema.enums import DetectEngine
from ..schema.results import ShotDetectionResult, ToolReceipt

# 代表切点的采信优先级：越靠前越优先被采纳
_ENGINE_PRIORITY = ("content", "adaptive", "hypit")
# 由 PySceneDetect 提供的引擎（用于 engine_used 的后端标注）
_PYSCENEDETECT_KINDS = ("content", "adaptive")


def _run_hypit(
    path: str, threshold: float | None, start_sec: float, end_sec: float | None
) -> _DetectorRun:
    """跑一次 hypit 边界探测，转成内部 run 结构。

    复用 Provider 自身的筛选语义（密集簇 / 低分簇不算硬切），**不在这里重复
    实现筛选规则**——规则的唯一事实来源在 Provider 里。
    """
    from ..providers.shots_hypit import HypitBoundaryDetector

    result = HypitBoundaryDetector().detect(
        path,
        engine=DetectEngine.HYPIT,
        threshold=threshold,
        start_sec=start_sec,
        end_sec=end_sec,
    )
    shots = result.shots
    boundaries = [
        (float(shot.in_sec), float(shot.confidence.boundary)) for shot in shots[1:]
    ]
    duration = float(shots[-1].out_sec) if shots else 0.0
    return _DetectorRun(
        kind="hypit",
        duration_sec=duration,
        fps=0.0,
        boundaries=boundaries,
        warnings=list(result.receipt.warnings),
    )


def _cross_validate_multi(runs: list[_DetectorRun]) -> list[_Boundary]:
    """N 路交叉验证：一致处采信，分歧处显式标记。"""
    points = sorted(
        [(sec, score, run.kind) for run in runs for (sec, score) in run.boundaries],
        key=lambda p: p[0],
    )
    engines = sorted({run.kind for run in runs})
    adopted: list[_Boundary] = []
    i, n = 0, len(points)
    while i < n:
        cluster = [points[i]]
        j = i + 1
        while j < n and points[j][0] - points[i][0] <= MATCH_TOL:
            cluster.append(points[j])
            j += 1
        hit = {p[2] for p in cluster}
        # 代表切点：按采信优先级取第一个命中的引擎
        chosen = None
        for kind in _ENGINE_PRIORITY:
            if kind in hit:
                chosen = next(p for p in cluster if p[2] == kind)
                break
        if chosen is None:  # pragma: no cover - 命中集合非空时必能找到
            chosen = max(cluster, key=lambda p: p[1])
        rep_sec = chosen[0]
        # 分数取簇内最高分：那是该处最强的变化证据
        rep_score = max(p[1] for p in cluster)
        boundary = _Boundary(sec=rep_sec, score=rep_score)
        missing = set(engines) - hit
        if missing:
            boundary.disagreement = True
            boundary.reason = (
                f"{'多' if len(engines) > 2 else '双'}引擎边界不一致："
                f"{'、'.join(sorted(hit))} 在 {rep_sec:.3f}s 检测到切点，"
                f"{'、'.join(sorted(missing))} 在 ±{MATCH_TOL:.1f}s 内无匹配"
            )
        adopted.append(boundary)
        i = j
    return adopted


async def detect_shots(
    path: str,
    engine: DetectEngine = DetectEngine.AUTO,
    threshold: float | None = None,
    start_sec: float = 0.0,
    end_sec: float | None = None,
    extra_engines: list[str] | None = None,
) -> ShotDetectionResult:
    """检测镜头边界；多引擎交叉验证，分歧显式暴露。

    参数:
        path: 视频文件路径。
        engine: 主引擎。auto=content+adaptive 交叉；content/adaptive=单引擎；
            hypit=可选外部引擎（本机需装 @hypit/hypit）。
        threshold: 边界判定阈值；None=交由引擎自定。
        start_sec: 分析起始时间（秒）。
        end_sec: 分析结束时间（秒）；None=到片尾。
        extra_engines: 额外参与交叉验证的引擎名（如 ["hypit"]）。None=不追加。

    失败时会怎样:
        - transnet 引擎 -> 抛 NotImplementedError（Phase 2）。
        - 指定 hypit 但本机未安装 -> 抛异常并给出安装提示，**绝不静默回退**。
        - 各引擎切点不一致 -> 写入 disagreements，等待人工或下游复核。
    """
    if engine == DetectEngine.TRANSNET:
        raise NotImplementedError("detect_shots: transnet 引擎未实现（Phase 2）")

    plan: list[DetectEngine] = (
        [DetectEngine.CONTENT, DetectEngine.ADAPTIVE]
        if engine == DetectEngine.AUTO
        else [engine]
    )
    for extra in extra_engines or []:
        plan.append(DetectEngine(extra))

    started = datetime.now()
    t0 = time.perf_counter()

    runs: list[_DetectorRun] = []
    for eng in plan:
        if eng in (DetectEngine.CONTENT, DetectEngine.ADAPTIVE):
            runs.append(
                run_pyscenedetect(path, eng.value, threshold, start_sec, end_sec)
            )
        elif eng == DetectEngine.HYPIT:
            runs.append(_run_hypit(path, threshold, start_sec, end_sec))
        else:  # pragma: no cover - 枚举里暂无其它引擎
            raise NotImplementedError(f"detect_shots: 引擎 {eng.value} 未实现")

    duration = max(r.duration_sec for r in runs)
    adopted = _cross_validate_multi(runs)

    kinds = [r.kind for r in runs]
    engine_used = "+".join(kinds)
    if all(kind in _PYSCENEDETECT_KINDS for kind in kinds):
        engine_used = f"pyscenedetect:{engine_used}"

    shots, disagreements, shot_warnings = _build_shots(adopted, start_sec, duration)
    warnings = [w for r in runs for w in r.warnings] + shot_warnings

    receipt = ToolReceipt(
        tool="detect_shots",
        params={
            "path": path,
            "engine": engine.value,
            "extra_engines": list(extra_engines or []),
            "threshold": threshold,
            "start_sec": start_sec,
            "end_sec": end_sec,
        },
        ok=True,
        started_at=started,
        duration_ms=int((time.perf_counter() - t0) * 1000),
        artifacts=[path],
        warnings=warnings,
    )
    return ShotDetectionResult(
        path=path,
        engine_used=engine_used,
        shots=shots,
        disagreements=disagreements,
        receipt=receipt,
    )
