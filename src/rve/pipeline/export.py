"""导出编排：把某个 spec_id 对应的 EditSpec 导出成 NLE 可导入的时间线。

读取 spec 的唯一入口是 `rve.workspace.read_spec`（由另一并行模块提供）。
为兼容该模块尚未就位的情况，这里**延迟导入** read_spec —— 顶层 import 会让
本模块在 workspace 缺席时直接 import 失败，进而拖垮整份测试收集。

支持格式：
- fcp7xml：纯标准库（FCP7 XML / xmeml）；
- edl：纯标准库（CMX3600）；
- otio：依赖可选包 opentimelineio，**缺包时抛带人话的 NotImplementedError**；
- fcpxml / resolve_xml：暂未实现，同样抛 NotImplementedError。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rve.schema.enums import ExportFormat
from rve.schema.results import ExportResult, ToolReceipt

if TYPE_CHECKING:
    from rve.schema.editspec import EditSpec

# 各格式默认输出的扩展名
_EXT = {
    ExportFormat.FCP7XML: ".fcp7xml",
    ExportFormat.OTIO: ".otio",
    ExportFormat.EDL: ".edl",
    ExportFormat.FCPXML: ".fcpxml",
    ExportFormat.RESOLVE_XML: ".xml",
}


def _default_out_path(spec_id: str, fmt: ExportFormat) -> Path:
    """默认产物路径：cwd/exports/<spec_id><ext>。"""
    return Path.cwd() / "exports" / f"{spec_id}{_EXT.get(fmt, '.bin')}"


def _frames_to_timecode(frame: int, fps: float) -> str:
    """帧号 → EDL 时间码 HH:MM:SS:FF（FF 用取整后的 timebase 计算）。"""
    tb = int(round(fps))
    total_sec = frame // tb
    sec = total_sec % 60
    minute = (total_sec // 60) % 60
    hour = (total_sec // 3600) % 24
    return f"{hour:02d}:{minute:02d}:{sec:02d}:{frame % tb:02d}"


def write_edl(
    spec: EditSpec, out_path: str | Path, fps_override: float | None = None
) -> Path:
    """把 EditSpec 写成 CMX3600 EDL（纯标准库，无第三方依赖）。

    - 每个镜头一条 V 轨事件：`NNN  AX       V     C        srcIn srcOut recIn recOut`；
    - 进入渐变过渡（dissolve / fade）的镜头用 `D` + 时长帧数，硬切用 `C`；
    - 源内时间码取 shot.in_sec/out_sec，录机端（rec）在序列时间轴上**累积**，
      与 fcp7 导出器保持同一套"单一时长换算"口径（复用其 `_sec_to_frames`）。
    """
    # 复用 fcp7 的唯一换算点，避免出现第二套时间码口径
    from rve.exporters.fcp7 import _GRADUAL, _resolve_fps, _sec_to_frames

    fps = _resolve_fps(spec, fps_override)
    lines = [f"TITLE: {spec.spec_id}", "FCM: NON-DROP FRAME", ""]

    rec = 0
    for idx, shot in enumerate(spec.shots, start=1):
        in_f = _sec_to_frames(shot.in_sec, fps)
        out_f = _sec_to_frames(shot.out_sec, fps)
        if out_f - in_f < 1:
            out_f = in_f + 1  # 退化保护：不足 1 帧时钳到 1 帧
        dur = out_f - in_f

        gradual = shot.cut_type in _GRADUAL and shot.transition is not None
        if gradual:
            d = max(1, _sec_to_frames(shot.transition.duration_sec, fps))
            trans_field = f"D{d:>5}".ljust(9)
        else:
            trans_field = "C".ljust(9)

        lines.append(
            f"{idx:03d}  {'AX':<8} {'V':<5} {trans_field} "
            f"{_frames_to_timecode(in_f, fps)} "
            f"{_frames_to_timecode(out_f, fps)} "
            f"{_frames_to_timecode(rec, fps)} "
            f"{_frames_to_timecode(rec + dur, fps)}"
        )
        rec += dur

    text = "\n".join(lines) + "\n"
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def export_timeline(
    spec_id: str,
    export_format: ExportFormat = ExportFormat.FCP7XML,
    out_path: str | None = None,
    fps_override: float | None = None,
) -> ExportResult:
    """导出时间线。

    支持 FCP7XML（纯标准库）、EDL（CMX3600，纯标准库）与 OTIO（需装
    opentimelineio，缺包时降级为带人话的 NotImplementedError）三种格式；
    其余格式（FCPXML/RESOLVE_XML）暂未实现，直接抛 NotImplementedError，
    以免悄悄产出不可用的半成品。
    """
    from rve.workspace import read_spec  # 延迟导入：workspace 由并行模块提供

    started = datetime.now()
    fmt = ExportFormat(export_format)
    spec = read_spec(spec_id)

    warnings: list[str] = []
    target = str(out_path) if out_path is not None else str(_default_out_path(spec_id, fmt))

    if fmt == ExportFormat.FCP7XML:
        from rve.exporters.fcp7 import write_fcp7_xml

        written = write_fcp7_xml(spec, target, fps_override=fps_override)
        tool = "write_fcp7_xml"
    elif fmt == ExportFormat.EDL:
        written = write_edl(spec, target, fps_override=fps_override)
        tool = "write_edl"
    elif fmt == ExportFormat.OTIO:
        from rve.exporters.otio_export import write_otio

        written = write_otio(spec, target)
        tool = "write_otio"
    else:
        raise NotImplementedError(f"暂不支持的导出格式: {fmt.value}")

    if fps_override is not None:
        warnings.append(f"使用 fps_override={fps_override} 覆盖源帧率 {spec.source.fps}")

    duration_ms = int((datetime.now() - started).total_seconds() * 1000)
    receipt = ToolReceipt(
        tool=tool,
        params={
            "spec_id": spec_id,
            "export_format": fmt.value,
            "fps_override": fps_override,
        },
        ok=True,
        started_at=started,
        duration_ms=duration_ms,
        artifacts=[str(written)],
        warnings=warnings,
    )
    return ExportResult(format=fmt.value, out_path=str(written), receipt=receipt)
