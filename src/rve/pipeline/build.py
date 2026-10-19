"""汇聚步骤：把 probe / shots / audio（可选 descriptions）汇聚为 EditSpec 并落盘。

红线：EditSpec 构造失败必须让异常抛出，禁止 try/except 吞掉。
"""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from datetime import datetime

from .. import workspace
from ..schema.editspec import EditSpec, Provenance, Rhythm
from ..schema.enums import CutCurve
from ..schema.results import (
    AudioAnalysisResult,
    BuildSpecResult,
    ShotDescriptionResult,
    ShotDetectionResult,
    ToolReceipt,
    VideoProbeResult,
)

# 末镜终点与源时长的允许对齐容差（秒）：容器时长与帧时长差在此范围内则吸附
_TAIL_SNAP_TOL = 0.5


def _default_spec_id() -> str:
    """生成缺省 spec_id：日期 + 6 位随机后缀。"""
    return datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6]


def _classify_cut_curve(shots, duration_sec: float) -> CutCurve:
    """按前 1/3 与后 1/3 的平均镜头长度粗判节奏曲线。"""
    if len(shots) < 3 or duration_sec <= 0:
        return CutCurve.UNKNOWN
    third = duration_sec / 3.0
    first_durs: list[float] = []
    last_durs: list[float] = []
    for shot in shots:
        mid = (shot.in_sec + shot.out_sec) / 2.0
        if mid < third:
            first_durs.append(shot.duration_sec)
        elif mid > 2 * third:
            last_durs.append(shot.duration_sec)
    if not first_durs or not last_durs:
        return CutCurve.UNKNOWN
    avg_first = sum(first_durs) / len(first_durs)
    avg_last = sum(last_durs) / len(last_durs)
    if avg_first <= 0:
        return CutCurve.UNKNOWN
    ratio = avg_last / avg_first
    if ratio >= 1.3:
        return CutCurve.FAST_START
    if ratio <= 0.77:
        return CutCurve.SLOW_BUILD
    return CutCurve.STEADY


def _compute_rhythm(shots, duration_sec: float) -> Rhythm:
    """统计镜头节奏。cuts_per_min = 镜头数 / 分钟数。"""
    if not shots:
        return Rhythm()
    durations = [shot.duration_sec for shot in shots]
    count = len(shots)
    cuts_per_min = count / (duration_sec / 60.0) if duration_sec > 0 else 0.0
    return Rhythm(
        shot_count=count,
        avg_shot_sec=sum(durations) / count,
        median_shot_sec=statistics.median(durations),
        shortest_shot_sec=min(durations),
        longest_shot_sec=max(durations),
        cuts_per_min=cuts_per_min,
        cut_curve=_classify_cut_curve(shots, duration_sec),
    )


def _apply_descriptions(shots, descriptions: ShotDescriptionResult) -> None:
    """按镜头索引写入描述与 OCR 文本。"""
    for shot in shots:
        text = descriptions.descriptions.get(shot.index)
        if text:
            shot.description = text
        ocr = descriptions.ocr.get(shot.index)
        if ocr:
            shot.ocr_text = list(ocr)


def _snap_tail(shots, duration_sec: float) -> None:
    """把末镜终点吸附到源时长，保证“铺满时间轴”不变量成立。

    检测器用帧数推算时长，容器时长会略有出入；差异在容差内时以权威源时长为准。
    """
    if not shots:
        return
    last = max(shots, key=lambda shot: shot.out_sec)
    gap = abs(last.out_sec - duration_sec)
    if 0 < gap <= _TAIL_SNAP_TOL:
        last.out_sec = duration_sec


async def build_edit_spec(
    probe: VideoProbeResult,
    shots: ShotDetectionResult,
    audio: AudioAnalysisResult,
    descriptions: ShotDescriptionResult | None = None,
    spec_id: str | None = None,
) -> BuildSpecResult:
    """汇聚为 EditSpec 并写入工作区，返回带回执的结果。"""
    started = datetime.now()
    t0 = time.perf_counter()
    resolved_id = spec_id or _default_spec_id()

    # 深拷贝，避免污染调用方传入的检测结果
    shot_list = [shot.model_copy(deep=True) for shot in shots.shots]
    if descriptions is not None:
        _apply_descriptions(shot_list, descriptions)
    _snap_tail(shot_list, probe.source.duration_sec)

    rhythm = _compute_rhythm(shot_list, probe.source.duration_sec)
    uncertain = [item.model_copy(deep=True) for item in shots.disagreements]
    warnings = list(shots.receipt.warnings) + list(probe.receipt.warnings)

    # EditSpec 校验失败必须抛出，绝不 try/except 吞掉
    spec = EditSpec(
        spec_id=resolved_id,
        source=probe.source,
        shots=shot_list,
        audio=audio.audio,
        rhythm=rhythm,
        uncertain=uncertain,
        warnings=warnings,
        provenance=[
            Provenance(
                tool="rve.pipeline.build",
                params={"shots_engine": shots.engine_used},
            )
        ],
    )

    path = workspace.write_spec(spec)
    receipt = ToolReceipt(
        tool="build_edit_spec",
        params={
            "spec_id": resolved_id,
            "source": probe.source.path,
            "shot_count": len(shot_list),
        },
        ok=True,
        started_at=started,
        duration_ms=int((time.perf_counter() - t0) * 1000),
        artifacts=[str(path)],
        warnings=warnings,
    )
    return BuildSpecResult(spec_id=resolved_id, spec=spec, receipt=receipt)


async def get_edit_spec(spec_id: str) -> EditSpec:
    """从工作区读取 EditSpec。"""
    return await asyncio.to_thread(workspace.read_spec, spec_id)
