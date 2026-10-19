"""OTIO 导出器 —— opentimelineio **延迟导入**。

关键约束：本模块顶层**绝不 import otio**，这样在未安装 opentimelineio 的环境里
模块仍可被 import，依赖 otio 的测试用 `pytest.importorskip("opentimelineio")` 跳过。

我们把 EditSpec 里的秒按源帧率换算为帧，再写成 OTIO 的 RationalTime
（RationalTime 的 value 以 rate 为单位，不能直接塞秒 —— 见 `_rt`），并保留
dissolve / fade 为 OTIO 的 Transition，使 OTIO 能再适配到其它 NLE 格式。
"""

from __future__ import annotations

from pathlib import Path

from rve.schema.editspec import EditSpec
from rve.schema.enums import CutType

# 进入该镜头的渐变过渡（映射为 OTIO Transition）
_GRADUAL = (CutType.DISSOLVE, CutType.FADE)


def _require_otio():
    """延迟导入并返回 opentimelineio 模块。

    **缺包时抛带人话的 NotImplementedError**（而非裸 ImportError）—— 这是
    pipeline/export.py 的既有契约：可选依赖缺失属于"该能力当前不可用"，
    CLI 会据此返回退出码 2 并打印人话提示，绝不静默产出半成品。
    """
    try:
        import opentimelineio as otio  # noqa: PLC0415 —— 刻意延迟导入
    except ImportError as exc:
        raise NotImplementedError(
            "OTIO 导出需要可选依赖 opentimelineio，请先安装："
            "pip install 'opentimelineio>=0.17'"
        ) from exc

    return otio


def _transition_type(otio, cut_type: CutType):
    """把 CutType 映射为 OTIO TransitionType 常量，带回退兜底。"""
    types = otio.schema.TransitionTypes
    if cut_type == CutType.FADE:
        return getattr(types, "TransitionType_Fade", "Fade")
    return getattr(types, "TransitionType_Dissolve", "Dissolve")


def _rt(otio, seconds: float, rate: float):
    """秒 → RationalTime（rate 取源帧率）。

    ⚠️ OTIO 的 `RationalTime(value, rate)` 中 **value 是以 rate 为单位的计数**：
    rate 取帧率时 value 就是帧号，`RationalTime(4.209, 24)` 表示 4.209 **帧**，
    即 0.175s，而不是 4.209s。直接把秒塞进 value 会让整条时间轴缩到 1/fps
    （实测 61.808s 的成片读回只有 2.575s）。所以这里必须先乘帧率。
    """
    return otio.opentime.RationalTime(float(seconds) * rate, rate)


def _source_reference(otio, spec: EditSpec):
    """源素材引用：C 型场景下所有镜头共享同一个源文件。

    必须显式写 ExternalReference 并带 available_range —— 否则 OTIO 会给 Clip
    默认挂一个 MissingReference，导出的 .otio 在 NLE 里重链不回原片（媒体全丢）。
    """
    rate = float(spec.source.fps)
    return otio.schema.ExternalReference(
        target_url=str(spec.source.path),
        available_range=otio.opentime.TimeRange(
            start_time=_rt(otio, 0.0, rate),
            duration=_rt(otio, spec.source.duration_sec, rate),
        ),
    )


def to_otio(spec: EditSpec):
    """把 EditSpec 转成 opentimelineio.schema.Timeline（含 video / audio 轨）。"""
    otio = _require_otio()
    rate = float(spec.source.fps)

    timeline = otio.schema.Timeline(name=spec.spec_id)
    video_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)

    for shot in spec.shots:
        # 渐变过渡（进入本镜头）：index>0 才有"相邻"可言
        if shot.cut_type in _GRADUAL and shot.index > 0 and shot.transition is not None:
            trans = otio.schema.Transition(name=str(shot.cut_type))
            trans.transition_type = _transition_type(otio, shot.cut_type)
            half = shot.transition.duration_sec / 2.0
            trans.in_offset = _rt(otio, half, rate)
            trans.out_offset = _rt(otio, half, rate)
            video_track.append(trans)

        clip = otio.schema.Clip(name=f"shot_{shot.index}")
        # 源在素材中的区间（C 型：源素材即原片）
        clip.source_range = otio.opentime.TimeRange(
            start_time=_rt(otio, shot.in_sec, rate),
            duration=_rt(otio, shot.duration_sec, rate),
        )
        # 挂回源素材，否则导出物无法在 NLE 里重链媒体
        clip.media_reference = _source_reference(otio, spec)
        video_track.append(clip)

    timeline.tracks.append(video_track)

    # 音频轨骨架：源含音频时补一条空轨，保证 OTIO 结构完整
    if spec.source.has_audio:
        audio_track = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)
        timeline.tracks.append(audio_track)

    return timeline


def write_otio(spec: EditSpec, out_path: str | Path, fmt: str = "otio") -> Path:
    """把 spec 写成 OTIO 文件；fmt 为适配器名（如 "otio" / "fcp_xml"）。

    默认按扩展名推断适配器；显式传 adapter_name 时以 fmt 为准。
    """
    otio = _require_otio()
    timeline = to_otio(spec)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt and fmt != "otio":
        otio.adapters.write_to_file(timeline, str(path), adapter_name=fmt)
    else:
        otio.adapters.write_to_file(timeline, str(path))
    return path
