"""PySceneDetect 镜头检测实现。

要点：
- 逐帧记录 detector 分数，用于估算镜头边界置信度（content_val 归一化）。
- engine=auto 时 ContentDetector 与 AdaptiveDetector 各跑一次做交叉验证：
  一致（±0.2s）处采信；不一致处写入 ShotDetectionResult.disagreements，绝不静默取其一。
- 首镜起点拉到 start_sec、末镜终点拉到视频总时长，保证 EditSpec“铺满时间轴”不变量成立。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from ..schema.editspec import Shot, ShotConfidence, UncertainItem
from ..schema.enums import CutType, DetectEngine
from ..schema.results import ShotDetectionResult, ToolReceipt

# 双引擎边界“视为同一点”的窗口（秒）
MATCH_TOL = 0.2
# 边界置信度低于此值视为偏低，需通过返回值（disagreements / warnings）暴露
LOW_CONF = 0.5
# 相邻边界最小间隔（秒），避免零长镜头
MIN_SHOT = 1e-3
# ContentDetector 默认阈值，用于把 content_val 归一化到 [0,1]
_DEFAULT_CONTENT_THRESHOLD = 27.0


def _pyscenedetect_version() -> str:
    try:
        import scenedetect

        return str(getattr(scenedetect, "__version__", "unknown"))
    except Exception:  # pragma: no cover - 仅在缺依赖时触发
        return "unknown"


@dataclass
class _Boundary:
    """一个切点及其元信息。"""

    sec: float
    score: float
    disagreement: bool = False
    reason: str = ""


@dataclass
class _DetectorRun:
    """一次单引擎检测的原始产物。"""

    kind: str
    duration_sec: float
    fps: float
    boundaries: list[tuple[float, float]] = field(default_factory=list)


def _normalize_score(raw: float | None, base: float) -> float:
    """把 detector 原始分数归一化为 [0,1]；拿不到分数时给保守值。"""
    if raw is None:
        return LOW_CONF
    denom = 2.0 * base
    if denom <= 0:
        return LOW_CONF
    return max(0.0, min(1.0, raw / denom))


def _run_detector(
    path: str,
    kind: str,
    threshold: float | None,
    start_sec: float,
    end_sec: float | None,
) -> _DetectorRun:
    """跑一次单引擎检测（content|adaptive），返回过滤后的内部切点集合。"""
    try:
        from scenedetect import (
            AdaptiveDetector,
            ContentDetector,
            SceneManager,
            open_video,
        )
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 scenedetect（pip install scenedetect）") from exc

    base_cls = ContentDetector if kind == "content" else AdaptiveDetector
    score_base = threshold if threshold is not None else _DEFAULT_CONTENT_THRESHOLD
    extra_kwargs: dict = {}
    if threshold is not None:
        extra_kwargs = (
            {"threshold": threshold}
            if kind == "content"
            else {"adaptive_threshold": threshold}
        )

    class _ScoredDetector(base_cls):  # type: ignore[misc, valid-type]
        """记录逐帧分数的子类，用于边界置信度。"""

        def __init__(self) -> None:
            super().__init__(**extra_kwargs)
            self.frame_scores: dict[int, float] = {}

        def process_frame(self, frame_num, frame_img):
            result = super().process_frame(frame_num, frame_img)
            raw = getattr(self, "_frame_score", None)
            if raw is not None:
                self.frame_scores[int(frame_num)] = float(raw)
            return result

    # 每个检测器都用独立视频流：复用同一 VideoStream 会在第二次检测时读到 EOF
    video = open_video(path)
    fps = float(video.frame_rate)
    duration = float(video.duration.seconds)

    manager = SceneManager()
    detector = _ScoredDetector()
    manager.add_detector(detector)
    manager.detect_scenes(video, show_progress=False)
    scenes = manager.get_scene_list(start_in_scene=True)

    end_limit = end_sec if end_sec is not None else duration
    boundaries: list[tuple[float, float]] = []
    for scene in scenes[1:]:  # 内部切点 = 每个场景（首个除外）的起点
        sec = float(scene[0].seconds)
        if not (start_sec < sec < end_limit):
            continue
        frame = scene[0].frame_num
        raw = detector.frame_scores.get(frame)
        if raw is None and frame > 0:
            raw = detector.frame_scores.get(frame - 1)
        boundaries.append((sec, _normalize_score(raw, score_base)))
    return _DetectorRun(kind, duration, fps, boundaries)


def _single_engine_boundaries(run: _DetectorRun) -> list[_Boundary]:
    return [_Boundary(sec=sec, score=score) for sec, score in run.boundaries]


def _cross_validate(content: _DetectorRun, adaptive: _DetectorRun) -> list[_Boundary]:
    """交叉验证两个引擎的切点：一致处采信，分歧处显式标记。"""
    points = sorted(
        [(sec, score, "content") for sec, score in content.boundaries]
        + [(sec, score, "adaptive") for sec, score in adaptive.boundaries],
        key=lambda p: p[0],
    )
    adopted: list[_Boundary] = []
    i, n = 0, len(points)
    while i < n:
        cluster = [points[i]]
        j = i + 1
        while j < n and points[j][0] - points[i][0] <= MATCH_TOL:
            cluster.append(points[j])
            j += 1
        kinds = {p[2] for p in cluster}
        content_member = next((p for p in cluster if p[2] == "content"), None)
        rep_sec = content_member[0] if content_member else cluster[0][0]
        rep_score = sum(p[1] for p in cluster) / len(cluster)
        boundary = _Boundary(sec=rep_sec, score=rep_score)
        if len(kinds) == 1:
            only = next(iter(kinds))
            other = "adaptive" if only == "content" else "content"
            boundary.disagreement = True
            boundary.reason = (
                f"双引擎边界不一致：{only} 在 {rep_sec:.3f}s 检测到切点，"
                f"{other} 在 ±{MATCH_TOL:.1f}s 内无匹配"
            )
        adopted.append(boundary)
        i = j
    return adopted


def _build_shots(
    boundaries: list[_Boundary],
    start_sec: float,
    duration_sec: float,
) -> tuple[list[Shot], list[UncertainItem], list[str]]:
    """把切点拼成连续、铺满时间轴的镜头，并收集分歧与低置信度告警。"""
    # 组装时间轴节点：起点 → 过滤后的内部切点 → 视频总时长
    tiling: list[float] = [start_sec]
    metas: list[_Boundary | None] = [None]
    for boundary in boundaries:
        if boundary.sec - tiling[-1] < MIN_SHOT:
            continue
        if duration_sec - boundary.sec < MIN_SHOT:
            continue
        tiling.append(boundary.sec)
        metas.append(boundary)
    tiling.append(duration_sec)
    metas.append(None)

    shots: list[Shot] = []
    disagreements: list[UncertainItem] = []
    warnings: list[str] = []
    for idx in range(len(tiling) - 1):
        meta = metas[idx]
        if meta is None:
            # 时间轴起点（首镜）：边界确定
            confidence = ShotConfidence(boundary=1.0)
        else:
            confidence = ShotConfidence(boundary=meta.score)
            if meta.disagreement:
                disagreements.append(
                    UncertainItem(
                        field_path=f"shots[{idx}].cut_type",
                        reason=meta.reason,
                        candidates=["content", "adaptive"],
                    )
                )
            if meta.score < LOW_CONF:
                warnings.append(
                    f"shots[{idx}].cut_type 边界置信度偏低({meta.score:.2f})"
                )
        shots.append(
            Shot(
                index=idx,
                in_sec=tiling[idx],
                out_sec=tiling[idx + 1],
                cut_type=CutType.HARD,
                confidence=confidence,
            )
        )
    return shots, disagreements, warnings


class PySceneDetectShotDetector:
    """基于 PySceneDetect 的镜头检测实现（满足 ShotDetector 协议）。"""

    tool_name = "pyscenedetect"

    def detect(
        self,
        path: str,
        engine: DetectEngine = DetectEngine.AUTO,
        threshold: float | None = None,
        start_sec: float = 0.0,
        end_sec: float | None = None,
    ) -> ShotDetectionResult:
        if engine == DetectEngine.TRANSNET:
            raise NotImplementedError(
                "detect_shots: transnet 引擎未实现（Phase 2）"
            )

        started = datetime.now()
        t0 = time.perf_counter()

        if engine == DetectEngine.CONTENT:
            runs = [_run_detector(path, "content", threshold, start_sec, end_sec)]
        elif engine == DetectEngine.ADAPTIVE:
            runs = [_run_detector(path, "adaptive", threshold, start_sec, end_sec)]
        else:  # AUTO：双引擎交叉验证
            runs = [
                _run_detector(path, "content", threshold, start_sec, end_sec),
                _run_detector(path, "adaptive", threshold, start_sec, end_sec),
            ]

        duration = max(r.duration_sec for r in runs)
        if engine == DetectEngine.AUTO:
            adopted = _cross_validate(runs[0], runs[1])
            engine_used = f"{self.tool_name}:content+adaptive"
        else:
            adopted = _single_engine_boundaries(runs[0])
            engine_used = f"{self.tool_name}:{runs[0].kind}"

        shots, disagreements, warnings = _build_shots(
            adopted, start_sec, duration
        )

        receipt = ToolReceipt(
            tool=self.tool_name,
            params={
                "path": path,
                "engine": engine.value,
                "threshold": threshold,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "detector_version": _pyscenedetect_version(),
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
