"""hypit 镜头检测实现。

hypit（npm 全局包 @hypit/hypit）只提供「机械的相邻帧变化候选 + 强度分数」，
其自身明确声明 *never editorial shot labels*——因此本 Provider 只把 hypit 当作
「变化强度证据源」，不把它的候选直接当镜头边界用。

要点：
- 一切外部调用走子进程（hypit 是 JS CLI，不 vendor 上游源码）。
- `hypit media boundaries --json` **不返回**时长与帧率，另用
  `hypit media probe --json` 取（实测字段名 duration / frameRate）。
- 候选按「相邻候选间隔 ≤ MATCH_TOL」聚簇：硬切表现为**孤立的高分候选**
  （簇内只有 1 个），渐变区表现为**一串密集的低分候选**（成员多）。
  两条筛选规则把两者分开，详见 _select_hard_cuts。
- 被规则排除的簇必须显式写进 ToolReceipt.warnings，绝不静默丢弃。
- 转场类型（硬切/溶解/淡入淡出）由 pipeline/transitions.py 判定，本层不猜。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..schema.editspec import Shot, ShotConfidence
from ..schema.enums import CutType, DetectEngine
from ..schema.results import ShotDetectionResult, ToolReceipt
from .shots_pyscenedetect import MATCH_TOL, MIN_SHOT

# 硬切候选的最低变化强度：簇内最高分低于此值不作为硬切切点
MIN_SCORE = 0.25
# hypit 子进程超时（秒）
_HYPIT_TIMEOUT = 300
# 失败信息里 stdout/stderr 尾部的截断长度
_OUTPUT_TAIL = 400
# hypit 未安装时的安装提示（hypit 是 RVE 的可选依赖）
_INSTALL_HINT = "npm i -g @hypit/hypit"
# hypit 未返回帧率时的保守占位（仅作记录，不参与任何计算）
_UNKNOWN_FPS = 0.0


@dataclass
class HypitCandidate:
    """一个机械变化候选：发生时刻（秒）与变化强度分数。"""

    sec: float
    score: float


@dataclass
class HypitCluster:
    """一组相邻候选构成的簇。

    sec = 簇内最高分候选的时间（簇代表切点）；score = 簇内最高分；
    member_count = 簇内候选数；members = 簇内全部候选。
    """

    sec: float
    score: float
    member_count: int
    members: list[HypitCandidate] = field(default_factory=list)


@dataclass
class HypitRun:
    """一次 hypit 运行的产物。

    clusters = 本次关注窗口内的簇；raw = hypit 返回的全部候选（未过滤，便于复核）。
    """

    duration_sec: float
    fps: float
    sample_rate: float  # hypit 的采样率（samples/s，实测 12）
    threshold: float  # hypit 自己的候选阈值（实测 0.1）
    clusters: list[HypitCluster] = field(default_factory=list)
    raw: list[HypitCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _to_float(value: object) -> float | None:
    """把 JSON 里的数值字段转成 float；类型不符返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _which_hypit() -> str:
    """定位 hypit 可执行文件；找不到时抛 RuntimeError（说明这是可选依赖）。"""
    exe = shutil.which("hypit")
    if not exe:
        raise RuntimeError(
            "未找到 hypit 可执行文件。hypit 是 RVE 的**可选**依赖，"
            f"安装命令：{_INSTALL_HINT}（安装后需确保 hypit 在 PATH 中，"
            f"可用 `hypit --version` 自检）。"
        )
    return exe


def _describe_failure(stdout: str, stderr: str) -> str:
    """把子进程失败信息整理成可读的尾部摘要。

    实测：hypit 出错时 **stderr 为空**，错误以 JSON 信封写在 stdout
    （形如 {"format": "hypit.cli-error@1", "ok": false, "error": {...}}），
    因此优先从 stdout 里取错误消息，再退回原始尾部。
    """
    text = (stderr or "").strip()
    stdout = (stdout or "").strip()
    if stdout.startswith("{"):
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
                code = str(error.get("code") or "").strip()
                detail = f"{code}: {message}" if code else message
            else:
                detail = ""
            if detail:
                text = f"{detail} | stderr: {text}" if text else detail
    if not text:
        text = stdout[-_OUTPUT_TAIL:] or "（无输出）"
    return text[-_OUTPUT_TAIL:]


def _run_hypit_json(exe: str, args: list[str]) -> dict:
    """执行 hypit 子进程并解析其 --json 输出；失败一律抛 RuntimeError。"""
    cmd = [exe, *args, "--json"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_HYPIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"hypit 执行超时（>{_HYPIT_TIMEOUT}s）: {' '.join(cmd)}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"hypit 执行失败: {' '.join(cmd)}: {exc}") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"hypit 失败(rc={proc.returncode}): "
            f"{_describe_failure(proc.stdout or '', proc.stderr or '')}"
        )
    try:
        data = json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"hypit 输出非合法 JSON: {(proc.stdout or '')[:_OUTPUT_TAIL]!r}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"hypit 输出不是 JSON 对象: {data!r}"[:_OUTPUT_TAIL])
    return data


def _parse_probe(data: dict) -> tuple[float, float, list[str]]:
    """解析 `hypit media probe --json`，取时长与帧率。

    实测字段名：path / duration / hasVideo / width / height / frameRate / hasAudio。
    取不到时长是硬错误（无法铺满时间轴）；取不到帧率给保守占位并记 warning。
    """
    warnings: list[str] = []
    duration = _to_float(data.get("duration"))
    if duration is None or duration <= 0:
        raise RuntimeError(
            f"hypit media probe 未返回有效时长(duration): {str(data)[:_OUTPUT_TAIL]}"
        )
    fps = _to_float(data.get("frameRate"))
    if fps is None or fps <= 0:
        fps = _UNKNOWN_FPS
        warnings.append(
            "hypit media probe 未返回有效帧率(frameRate)，fps 记为 0（不影响切点判定）"
        )
    return duration, fps, warnings


def _parse_boundaries(data: dict) -> tuple[list[HypitCandidate], float, float, list[str]]:
    """解析 `hypit media boundaries --json`。

    实测字段名：path / sampleRate / threshold / candidates[].at / candidates[].score。
    返回 (候选列表, 采样率, 工具阈值, 告警)。
    """
    warnings: list[str] = []
    raw_candidates = data.get("candidates")
    if raw_candidates is None:
        raw_candidates = []
    if not isinstance(raw_candidates, list):
        raise RuntimeError(
            f"hypit boundaries 输出异常：candidates 不是数组: {raw_candidates!r}"[
                :_OUTPUT_TAIL
            ]
        )

    candidates: list[HypitCandidate] = []
    for item in raw_candidates:
        if not isinstance(item, dict) or "at" not in item or "score" not in item:
            raise RuntimeError(
                f"hypit boundaries 候选字段缺失(需 at/score): {item!r}"[:_OUTPUT_TAIL]
            )
        at = _to_float(item.get("at"))
        score = _to_float(item.get("score"))
        if at is None or score is None:
            raise RuntimeError(
                f"hypit boundaries 候选数值非法: {item!r}"[:_OUTPUT_TAIL]
            )
        candidates.append(HypitCandidate(sec=at, score=score))
    candidates.sort(key=lambda c: c.sec)

    sample_rate = _to_float(data.get("sampleRate"))
    if sample_rate is None:
        sample_rate = 0.0
        warnings.append("hypit boundaries 未返回采样率(sampleRate)，记为 0")
    threshold = _to_float(data.get("threshold"))
    if threshold is None:
        threshold = 0.0
        warnings.append("hypit boundaries 未返回候选阈值(threshold)，记为 0")
    return candidates, sample_rate, threshold, warnings


def _make_cluster(members: list[HypitCandidate]) -> HypitCluster:
    """由簇内候选构造簇：代表切点 = 最高分候选的时间，分数 = 最高分。"""
    best = max(members, key=lambda c: c.score)
    return HypitCluster(
        sec=best.sec,
        score=best.score,
        member_count=len(members),
        members=list(members),
    )


def _cluster_candidates(candidates: list[HypitCandidate]) -> list[HypitCluster]:
    """按「相邻候选间隔 ≤ MATCH_TOL」把候选聚成簇。

    依据：hypit 的候选是机械的相邻帧变化量——硬切表现为**孤立的高分候选**
    （簇内只有 1 个），渐变区表现为**一串密集的低分候选**。
    窗口按「相邻候选」度量（链式），所以 0.083s 间隔的一串渐变候选会并成 1 簇。
    """
    clusters: list[HypitCluster] = []
    current: list[HypitCandidate] = []
    for cand in candidates:
        if current and cand.sec - current[-1].sec > MATCH_TOL:
            clusters.append(_make_cluster(current))
            current = []
        current.append(cand)
    if current:
        clusters.append(_make_cluster(current))
    return clusters


def _select_hard_cuts(
    clusters: list[HypitCluster],
) -> tuple[list[HypitCluster], list[str]]:
    """按规则 A/B 挑出可作为硬切切点的簇，返回 (采用簇, 告警列表)。

    规则 A（密度）：member_count >= 2 → 判为渐变区密集簇，不作为硬切切点；
    规则 B（强度）：簇最高分 < MIN_SCORE → 不作为硬切切点。
    两条规则都不通过的簇必须显式记录（写进 ToolReceipt.warnings），绝不静默丢弃。
    """
    kept: list[HypitCluster] = []
    warnings: list[str] = []
    for cluster in clusters:
        if cluster.member_count >= 2:
            warnings.append(
                f"shots[边界 {cluster.sec:.3f}s] 附近有 {cluster.member_count} 个密集候选"
                f"（最高分 {cluster.score:.2f}），判为渐变区，未作为硬切切点"
            )
            continue
        if cluster.score < MIN_SCORE:
            warnings.append(
                f"shots[边界 {cluster.sec:.3f}s] 附近候选变化强度偏低"
                f"（最高分 {cluster.score:.2f} < {MIN_SCORE}），未作为硬切切点"
            )
            continue
        kept.append(cluster)
    return kept, warnings


def _build_shots(
    cuts: list[HypitCluster],
    start_sec: float,
    duration_sec: float,
) -> list[Shot]:
    """把切点拼成连续、无重叠、铺满时间轴的镜头段。

    语义与 shots_pyscenedetect._build_shots 一致：首镜起点 = start_sec，
    末镜终点 = 视频总时长；过近的切点直接丢弃以避免零长镜头。
    """
    tiling: list[float] = [start_sec]
    metas: list[HypitCluster | None] = [None]
    for cut in cuts:
        if cut.sec - tiling[-1] < MIN_SHOT:
            continue
        if duration_sec - cut.sec < MIN_SHOT:
            continue
        tiling.append(cut.sec)
        metas.append(cut)
    tiling.append(duration_sec)
    metas.append(None)

    shots: list[Shot] = []
    for idx in range(len(tiling) - 1):
        meta = metas[idx]
        if meta is None:
            # 时间轴起点（首镜）：边界确定
            confidence = ShotConfidence(boundary=1.0)
        else:
            # 分数落在 [0,1]，越界值裁剪到区间内，避免下游校验失败
            score = max(0.0, min(1.0, meta.score))
            confidence = ShotConfidence(boundary=score)
        shots.append(
            Shot(
                index=idx,
                in_sec=tiling[idx],
                out_sec=tiling[idx + 1],
                cut_type=CutType.HARD,
                confidence=confidence,
            )
        )
    return shots


def _is_hypit_engine(engine: object) -> bool:
    """判断 engine 是否为 hypit。

    DetectEngine.HYPIT 由主线程新增；此处按值判定（而非硬引用枚举成员），
    避免枚举尚未落地时整个 Provider 直接不可用。
    """
    hypit_enum = getattr(DetectEngine, "HYPIT", None)
    if hypit_enum is not None and engine == hypit_enum:
        return True
    return getattr(engine, "value", engine) == "hypit"


class HypitBoundaryDetector:
    """基于 hypit 变化候选的镜头检测实现（满足 ShotDetector 协议）。"""

    tool_name = "hypit"

    def _collect(
        self,
        path: str,
        threshold: float | None,
    ) -> tuple[HypitRun, list[HypitCluster]]:
        """跑一次 hypit（probe + boundaries），返回 (运行摘要, 全部候选簇)。"""
        exe = _which_hypit()

        probe_data = _run_hypit_json(exe, ["media", "probe", path])
        duration, fps, probe_warnings = _parse_probe(probe_data)

        boundaries_args = ["media", "boundaries", path]
        threshold_warnings: list[str] = []
        if threshold is not None:
            if 0.0 <= threshold <= 1.0:
                boundaries_args += ["--threshold", str(threshold)]
            else:
                # 跨引擎共享的 threshold 参数在 PySceneDetect 侧是 content_val 量纲
                # （默认 27），而 hypit 的候选阈值只接受 0~1；越界值直接透传会让
                # hypit 以 rc=1 退出。此处改用 hypit 自己的默认阈值，并显式告警——
                # 宁可少一个可调项，也不让整次检测无理由失败。
                threshold_warnings.append(
                    f"threshold={threshold} 超出 hypit 候选阈值取值范围(0~1)，"
                    "已改用 hypit 默认阈值"
                )
        boundaries_data = _run_hypit_json(exe, boundaries_args)
        candidates, sample_rate, tool_threshold, boundary_warnings = _parse_boundaries(
            boundaries_data
        )

        run = HypitRun(
            duration_sec=duration,
            fps=fps,
            sample_rate=sample_rate,
            threshold=tool_threshold,
            clusters=[],
            raw=candidates,
            warnings=probe_warnings + threshold_warnings + boundary_warnings,
        )
        return run, _cluster_candidates(candidates)

    def run_boundaries(
        self,
        path: str,
        start_sec: float = 0.0,
        end_sec: float | None = None,
    ) -> HypitRun:
        """跑一次 hypit 变化候选检测，返回聚类结果（不做硬切筛选）。"""
        run, clusters = self._collect(path, None)
        run.clusters = [
            c for c in clusters if _in_window(c.sec, start_sec, end_sec)
        ]
        return run

    def detect(
        self,
        path: str,
        engine: DetectEngine = DetectEngine.AUTO,
        threshold: float | None = None,
        start_sec: float = 0.0,
        end_sec: float | None = None,
    ) -> ShotDetectionResult:
        """用 hypit 变化候选切镜。

        只实现 engine=hypit；其余引擎在本 Provider 内不实现（由各自的 Provider 负责）。
        """
        if not _is_hypit_engine(engine):
            raise NotImplementedError(
                "hypit provider 只实现 engine=hypit，收到 "
                f"{getattr(engine, 'value', engine)}"
            )

        started = datetime.now()
        t0 = time.perf_counter()

        run, clusters = self._collect(path, threshold)
        duration = run.duration_sec
        end_limit = end_sec if end_sec is not None else duration
        in_window = [
            c for c in clusters if start_sec < c.sec < end_limit
        ]
        kept, filter_warnings = _select_hard_cuts(in_window)
        run.clusters = kept
        warnings = list(run.warnings) + filter_warnings

        shots = _build_shots(kept, start_sec, duration)

        receipt = ToolReceipt(
            tool=self.tool_name,
            params={
                "path": path,
                "engine": getattr(engine, "value", engine),
                "threshold": threshold,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "sample_rate": run.sample_rate,
                "hypit_threshold": run.threshold,
                "min_score": MIN_SCORE,
                "match_tol": MATCH_TOL,
            },
            ok=True,
            started_at=started,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            artifacts=[path],
            warnings=warnings,
        )
        return ShotDetectionResult(
            path=path,
            engine_used=f"{self.tool_name}:boundaries",
            shots=shots,
            # 跨引擎分歧由主线程在 pipeline/detect.py 统一处理，本 Provider 不产出
            disagreements=[],
            receipt=receipt,
        )


def _in_window(sec: float, start_sec: float, end_sec: float | None) -> bool:
    """判断时间点是否落在 [start_sec, end_sec] 窗口内（end_sec 为 None 表示到片尾）。"""
    if sec < start_sec:
        return False
    if end_sec is not None and sec > end_sec:
        return False
    return True
