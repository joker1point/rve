"""转场识别：判定镜头边界是硬切 / 溶解 / 淡入淡出。

出发点：PySceneDetect 只能找到"帧间差出现跃变"的位置，把它们一律标成硬切；
但现实成片里大量边界是*b渐变*过渡（溶解 dissolve / 淡入淡出 fade），
只是"更缓的跃变"。本模块在候选边界附近**只采样一个小窗口**（不解码全片），
用逐帧像素差与亮度曲线把三类分开，并为每条结论附上证据与置信度。

判据（三类信号）：
- 硬切 HARD：窗口内几乎只有一个大的单帧阶跃（active 帧数 <= 2）。
- 溶解 DISSOLVE：变化铺在连续多帧上（active 帧数 >= 3），且亮度未塌陷到近黑。
- 淡入淡出 FADE：亮度在边界附近塌陷到近黑（fade through black / dip to black），
  且同样是渐变（active 帧数 >= 3）——用"渐变"把"硬切到黑场"排除掉。

红线：
- 证据不足（窗口太短 / 完全没有可辨识的变化）时返回 UNKNOWN 并标记 uncertain，
  绝不用默认值蒙混。
- 取帧只碰边界附近的窗口；单条 ffmpeg 调用只解码该窗口，保证快。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..schema.enums import CutType
from ..schema.results import ToolReceipt

# --------------------------------------------------------------------------- #
# 经验阈值（尚未在大量真实素材上验证，见模块末尾说明）
# --------------------------------------------------------------------------- #

# 边界两侧各采样的最大半窗时长（秒）
WINDOW_SEC = 1.0
# 采样分辨率（宽 x 高）；缩到极小即可，只为统计像素差
SAMPLE_W = 64
SAMPLE_H = 48
# active 帧的判定阈值下限（归一化像素差）；真实素材的基线噪声由 BASE_MULT 兜底
ABS_ELEV = 0.015
# 基线取窗口内像素差的第 N 百分位（而非中位数）：窗口内渐变可能占多数帧，
# 中位数会被渐变本身抬高，导致阈值反超信号
BASE_PERCENTILE = 10
# active 阈值 = 基线（窗口内像素差第 N 百分位）× 本倍数，取二者较大
BASE_MULT = 2.5
# active 连续帧数 <= 此值判为硬切
HARD_MAX_ACTIVE = 2
# active 帧数 >= 此值判为渐变（溶解 / 淡入淡出）
MIN_GRAD_FRAMES = 3
# 硬切单帧阶跃的下限；低于此值不敢断言
HARD_STEP_MIN = 0.12
# 硬切置信度归一化的参考阶跃
HARD_STEP_REF = 0.35
# 硬切的**相对**判据：单帧阶跃与该窗口自身运动噪声基线之比。
# 真实低对比硬切（两镜画面接近）的绝对阶跃可能只有 0.14~0.21，远低于 HARD_STEP_REF=0.35，
# 但相对窗口基线（~0.002）可达 50~150 倍——用绝对值单看会把这种"证据其实很足"的边界
# 误压成 uncertain。故置信度取"绝对值"与"相对信噪比"两者的较大者。
# 注意：本判据只影响**置信度**，不参与分支条件——绝不会凭空多认一个硬切。
HARD_RATIO_MIN = 6.0
HARD_RATIO_REF = 12.0
# 亮度塌陷判据：窗口内最低亮度 <= 此值视为"接近黑场"
FADE_DARK_LEVEL = 0.10
# 亮度跌落幅度下限：亮部与最暗处至少差这么多才认淡入淡出
FADE_DROP_MIN = 0.35
# 处于亮度"中段"（0.25~0.75 区间）的帧数下限：证明是渐变而非单帧跳黑
FADE_BAND_MIN = 2
# 单帧亮度跳变上限：超过它说明是"一步跳黑"（硬切）而非淡入淡出
FADE_MAX_STEP = 0.45
# 判定置信度低于此值时，结论照常给出，但显式进 uncertain[]
CONF_FLOOR = 0.5
# 渐变"铺满多久"即算满置信（秒）
GRAD_REF_SEC = 0.4
# 淡入淡出的亮度中段帧数达到多久即算满置信（秒）
FADE_REF_SEC = 0.25
# 窗口内可用的最少采样帧数；太少则证据不足
MIN_SAMPLES = 4


class TransitionSamplingError(RuntimeError):
    """取帧失败（缺 ffmpeg / 解码失败 / 路径不可读）。"""


@dataclass
class BoundaryVerdict:
    """一个镜头边界的判定结论。"""

    index: int
    sec: float
    cut_type: CutType
    confidence: float
    duration_sec: float
    uncertain: bool = False
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class TransitionDetectionResult:
    """整条视频所有边界转场判定的汇总，附带可复核回执。"""

    path: str
    verdicts: list[BoundaryVerdict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    receipt: ToolReceipt | None = None

    def by_index(self) -> dict[int, BoundaryVerdict]:
        return {v.index: v for v in self.verdicts}


# --------------------------------------------------------------------------- #
# 取帧与统计（与判定解耦，便于单测）
# --------------------------------------------------------------------------- #


def sample_window(
    path: str,
    start_sec: float,
    dur_sec: float,
    w: int = SAMPLE_W,
    h: int = SAMPLE_H,
) -> list[bytes]:
    """用一条 ffmpeg 命令取回窗口内的 RGB24 原始帧（不解码整片）。

    只解码 [start, start+dur) 这一段，缩到 w×h；返回每帧的原始字节。
    """
    if dur_sec <= 0:
        return []
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-ss", f"{max(0.0, start_sec):.3f}",
        "-i", path,
        "-t", f"{dur_sec:.3f}",
        "-an",
        "-vf", f"scale={w}:{h},format=rgb24",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError as exc:  # 缺 ffmpeg
        raise TransitionSamplingError("未找到 ffmpeg 可执行文件（需安装 ffmpeg）") from exc
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "ignore").strip()[:200]
        raise TransitionSamplingError(
            f"ffmpeg 取帧失败(rc={proc.returncode}): {err}"
        )
    fsize = w * h * 3
    raw = proc.stdout or b""
    n = len(raw) // fsize
    return [raw[i * fsize:(i + 1) * fsize] for i in range(n)]


def frame_stats(frames: Sequence[bytes]) -> tuple[list[float], list[float]]:
    """返回 (每帧平均亮度, 相邻帧平均绝对像素差)，均已归一化到 [0,1]。"""
    lum: list[float] = []
    for f in frames:
        lum.append((sum(f) / len(f) / 255.0) if f else 0.0)
    diffs: list[float] = []
    for a, b in zip(frames, frames[1:]):
        if not a or len(a) != len(b):
            diffs.append(0.0)
            continue
        diffs.append(sum(abs(x - y) for x, y in zip(a, b)) / len(a) / 255.0)
    return lum, diffs


def _longest_run(indices: Sequence[int]) -> int:
    """一列（升序）下标里最长连续区间的长度。"""
    best = 0
    run = 0
    prev: int | None = None
    for i in indices:
        run = run + 1 if prev is not None and i == prev + 1 else 1
        prev = i
        best = max(best, run)
    return best


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _percentile(values: Sequence[float], pct: float) -> float:
    """线性插值的百分位数（pct∈[0,100]）；values 非空。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    pos = (pct / 100.0) * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


# --------------------------------------------------------------------------- #
# 判定核心（纯函数，输入亮度/像素差曲线）
# --------------------------------------------------------------------------- #


def classify_window(lum: Sequence[float], diffs: Sequence[float], fps: float) -> BoundaryVerdict:
    """据亮度与像素差曲线判定边界类型（index/sec 由调用方回填）。

    返回结论携带 evidence（峰值差、基线、active 帧数、亮度极值等），供 receipt 记录。
    """
    n = len(lum)
    evidence: dict[str, Any] = {"frames": n, "fps": round(float(fps), 3)}

    if n < MIN_SAMPLES or len(diffs) < MIN_SAMPLES - 1:
        return BoundaryVerdict(
            index=-1, sec=0.0, cut_type=CutType.UNKNOWN, confidence=0.0,
            duration_sec=0.0, uncertain=True,
            reason=f"采样帧数不足（{n} 帧 < {MIN_SAMPLES}），无法判定",
            evidence=evidence,
        )

    baseline = _percentile(diffs, BASE_PERCENTILE)
    elev = max(baseline * BASE_MULT, ABS_ELEV)
    active_idx = [i for i, d in enumerate(diffs) if d > elev]
    active_run = _longest_run(active_idx)
    active_count = len(active_idx)
    # 渐变跨度：首个 active 与末个 active 之间（容忍近黑处的零差帧）
    span = (active_idx[-1] - active_idx[0] + 1) if active_idx else 0
    dmax = max(diffs)
    lum_min = min(lum)
    lum_max = max(lum)
    lum_range = lum_max - lum_min
    max_lum_step = max((abs(lum[i + 1] - lum[i]) for i in range(len(lum) - 1)), default=0.0)

    band_lo = lum_min + 0.25 * lum_range
    band_hi = lum_min + 0.75 * lum_range
    band_frames = sum(1 for v in lum if band_lo <= v <= band_hi)

    evidence.update(
        peak_diff=round(dmax, 4),
        baseline_diff=round(baseline, 4),
        # 单帧阶跃 / 窗口运动噪声基线；baseline 近 0 时无法定义比值，记 None
        contrast_ratio=round(dmax / baseline, 2) if baseline > 1e-6 else None,
        elev_threshold=round(elev, 4),
        active_frames=active_count,
        longest_active_run=active_run,
        active_span=span,
        luminance_min=round(lum_min, 4),
        luminance_max=round(lum_max, 4),
        luminance_range=round(lum_range, 4),
        max_lum_step=round(max_lum_step, 4),
        band_frames=band_frames,
    )

    # 完全无可辨识变化：证据不足，绝不猜
    if active_count == 0 and dmax <= elev and lum_range < FADE_DROP_MIN:
        return BoundaryVerdict(
            index=-1, sec=0.0, cut_type=CutType.UNKNOWN, confidence=0.0,
            duration_sec=0.0, uncertain=True,
            reason=(
                f"边界窗口内无显著帧间变化（峰值差 {dmax:.3f} <= 阈值 {elev:.3f}），"
                "无法区分硬切/渐变"
            ),
            evidence=evidence,
        )

    # 淡入淡出：亮度塌陷到近黑，且变化是渐进的（排除"一步跳黑"的硬切）
    fade_shape = (
        lum_min <= FADE_DARK_LEVEL
        and lum_range >= FADE_DROP_MIN
        and band_frames >= FADE_BAND_MIN
        and max_lum_step <= FADE_MAX_STEP
    )
    if fade_shape:
        conf = _clamp01(band_frames / max(2.0, FADE_REF_SEC * fps))
        duration = max(span, band_frames, 1) / fps
        return BoundaryVerdict(
            index=-1, sec=0.0, cut_type=CutType.FADE, confidence=conf,
            duration_sec=duration, uncertain=conf < CONF_FLOOR,
            reason="" if conf >= CONF_FLOOR else
            f"淡入淡出亮度塌陷证据偏弱（中段帧 {band_frames}，置信度 {conf:.2f}）",
            evidence=evidence,
        )

    # 溶解：变化铺在连续多帧上（且非亮度塌陷）
    if active_run >= MIN_GRAD_FRAMES and active_count >= MIN_GRAD_FRAMES:
        conf = _clamp01(active_run / max(2.0, GRAD_REF_SEC * fps))
        duration = max(span, active_run, 1) / fps
        return BoundaryVerdict(
            index=-1, sec=0.0, cut_type=CutType.DISSOLVE, confidence=conf,
            duration_sec=duration, uncertain=conf < CONF_FLOOR,
            reason="" if conf >= CONF_FLOOR else
            f"渐变铺展帧数偏少（{active_run} 帧，置信度 {conf:.2f}）",
            evidence=evidence,
        )

    # 硬切：单帧阶跃，且阶跃足够大
    if active_run <= HARD_MAX_ACTIVE and dmax >= HARD_STEP_MIN:
        # 置信度取"绝对阶跃强度"与"相对窗口噪声的信噪比"两者的较大者：
        # 低对比硬切绝对阶跃不大（0.14~0.21），但在静止/低运动窗口里信噪比极高（50 倍+），
        # 只看绝对值会把这些证据充足的边界误判为 uncertain。
        abs_norm = _clamp01((dmax - HARD_STEP_MIN) / (HARD_STEP_REF - HARD_STEP_MIN))
        has_baseline = baseline > 1e-6
        ratio = dmax / baseline if has_baseline else float("inf")
        ratio_norm = (
            _clamp01((ratio - HARD_RATIO_MIN) / (HARD_RATIO_REF - HARD_RATIO_MIN))
            if has_baseline else 1.0
        )
        conf = max(abs_norm, ratio_norm)
        reason = ""
        if conf < CONF_FLOOR:
            ratio_txt = f"，信噪比 {ratio:.1f}×" if has_baseline else ""
            reason = f"硬切阶跃偏弱（峰值差 {dmax:.3f}{ratio_txt}，置信度 {conf:.2f}）"
        return BoundaryVerdict(
            index=-1, sec=0.0, cut_type=CutType.HARD, confidence=conf,
            duration_sec=0.0, uncertain=conf < CONF_FLOOR,
            reason=reason,
            evidence=evidence,
        )

    # 有变化但既不像硬切也不像渐变：证据不足
    return BoundaryVerdict(
        index=-1, sec=0.0, cut_type=CutType.UNKNOWN, confidence=0.0,
        duration_sec=0.0, uncertain=True,
        reason=(
            f"变化形态不明确（active 帧 {active_count}，峰值差 {dmax:.3f}），"
            "不足以断言具体转场"
        ),
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# 编排：对每条边界取窗口 → 统计 → 判定
# --------------------------------------------------------------------------- #


def boundary_window(sec: float, prev_sec: float, next_sec: float, window_sec: float) -> tuple[float, float]:
    """在 [prev_sec, next_sec] 之间取以 sec 为中心、受相邻边界约束的窗口。

    半窗不超过相邻镜头各自的一半，避免采样跨进邻近转场。
    """
    half = min(
        window_sec,
        max(0.0, (sec - prev_sec) / 2.0),
        max(0.0, (next_sec - sec) / 2.0),
    )
    return sec - half, sec + half


def detect_transitions(
    path: str,
    boundaries: Sequence[tuple[int, float]],
    fps: float,
    duration_sec: float,
    *,
    window_sec: float = WINDOW_SEC,
) -> TransitionDetectionResult:
    """对每个候选边界判定 HARD / DISSOLVE / FADE，并给出证据与置信度。

    boundaries: (镜头索引, 边界秒) 列表；索引用于回填到 EditSpec.shots[i]。
    """
    started = datetime.now()
    t0 = time.perf_counter()
    warnings: list[str] = []
    verdicts: list[BoundaryVerdict] = []

    ordered = sorted(boundaries, key=lambda b: b[1])
    secs = [b[1] for b in ordered]
    records: list[dict[str, Any]] = []

    for pos, (index, sec) in enumerate(ordered):
        prev_sec = secs[pos - 1] if pos > 0 else 0.0
        next_sec = secs[pos + 1] if pos + 1 < len(secs) else duration_sec
        start, end = boundary_window(sec, prev_sec, next_sec, window_sec)
        start = max(0.0, start)
        end = min(duration_sec, end)
        try:
            frames = sample_window(path, start, end - start)
        except TransitionSamplingError as exc:
            warnings.append(f"shots[{index}] 转场取帧失败：{exc}")
            verdicts.append(
                BoundaryVerdict(
                    index=index, sec=sec, cut_type=CutType.UNKNOWN, confidence=0.0,
                    duration_sec=0.0, uncertain=True, reason=f"取帧失败：{exc}",
                    evidence={"window_sec": [round(start, 3), round(end, 3)]},
                )
            )
            continue

        lum, diffs = frame_stats(frames)
        verdict = classify_window(lum, diffs, fps)
        verdict.index = index
        verdict.sec = sec
        verdict.evidence["window_sec"] = [round(start, 3), round(end, 3)]
        verdicts.append(verdict)
        if verdict.uncertain:
            warnings.append(
                f"shots[{index}].cut_type 存疑（{verdict.cut_type.value}）：{verdict.reason}"
            )
        records.append(
            {
                "index": index,
                "sec": round(sec, 3),
                "cut_type": verdict.cut_type.value,
                "confidence": round(verdict.confidence, 3),
                "duration_sec": round(verdict.duration_sec, 3),
                **verdict.evidence,
            }
        )

    receipt = ToolReceipt(
        tool="rve.pipeline.transitions",
        params={
            "path": path,
            "boundary_count": len(ordered),
            "fps": fps,
            "window_sec": window_sec,
            "verdicts": records,
        },
        ok=True,
        started_at=started,
        duration_ms=int((time.perf_counter() - t0) * 1000),
        artifacts=[path],
        warnings=warnings,
    )
    return TransitionDetectionResult(
        path=path, verdicts=verdicts, warnings=warnings, receipt=receipt
    )
