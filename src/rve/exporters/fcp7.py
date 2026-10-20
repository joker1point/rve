"""FCP7 XML 导出器 —— 纯标准库实现（不依赖 opentimelineio）。

为什么纯标准库
--------------
FCP7 XML（xmeml）是 Premiere / DaVinci Resolve 都能吃的"最大公约数"时间线格式，
也是整个 C 型产品的验收落点。本模块刻意不 import 任何第三方库（连 otio 都不引），
从而在任何环境里都永远可 import、可测、可用；OTIO 路径见 otio_export.py。

命名空间决策
------------
xmeml 存在两种写法：带 XML namespace 与"裸标签"。Premiere / Resolve 两种都能吃，
我们选用**裸 xmeml（不带 namespace）**，因为它兼容面最广、最稳。

NTSC 处理（重要，这是唯一的换算坑）
------------------------------------
计算机视频的"整数帧率"与实际帧率之间存在 NTSC 场频带来的 ±0.1% 偏差：
    23.976 ≈ 24、29.97 ≈ 30、59.94 ≈ 60
FCP7 XML 用 <rate><timebase>（整数）+ <ntsc>（TRUE/FALSE）来表达这套帧率：
  - timebase 一律填**取整后的整数**（29.97 → 30），ntsc = TRUE；
  - 但时间码（<in>/<out>/<start>/<end>/<duration> 里的**帧号**）必须用**真实帧率**
    换算：frames = round(sec * 29.97)。若用取整后的 30 换算，长序列会累积漂移。
  - 非 NTSC（如 25/30/50/60 这类"整帧率"）则 timebase = round(fps)、ntsc = FALSE。

所有换算集中在 `_sec_to_frames` 与 `_rate_block` 两个内部函数里，构成单一事实来源。
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from rve.schema.editspec import EditSpec
from rve.schema.enums import CutType

# NTSC 的三种常见"非整数"帧率，判定容差 0.01
_NTSC_RATES = (23.976, 29.97, 59.94)
_NTSC_TOL = 0.01

# 渐变类过渡（进入该镜头的不是硬切）
_GRADUAL = (CutType.DISSOLVE, CutType.FADE)


# --------------------------------------------------------------------------- #
# 帧率与时间码换算（唯一换算点）
# --------------------------------------------------------------------------- #
def _resolve_fps(spec: EditSpec, fps_override: float | None) -> float:
    """确定本次导出使用的真实帧率：override 优先，否则用源视频帧率。"""
    if fps_override is not None:
        return float(fps_override)
    return float(spec.source.fps)


def _is_ntsc(fps: float) -> bool:
    """真实帧率是否落在 NTSC 非整数帧率集合内（容差 0.01）。"""
    return any(abs(fps - r) <= _NTSC_TOL for r in _NTSC_RATES)


def _sec_to_frames(sec: float, fps: float) -> int:
    """秒 → 帧号。对 NTSC 也使用**真实帧率**（不要用取整后的 timebase）。"""
    return int(round(sec * fps))


def _rate_block(fps: float) -> ET.Element:
    """构造 <rate> 块：<timebase> 为取整整数，<ntsc> 标记是否 NTSC。"""
    rate = ET.Element("rate")
    et_timebase = ET.SubElement(rate, "timebase")
    et_timebase.text = str(int(round(fps)))
    et_ntsc = ET.SubElement(rate, "ntsc")
    et_ntsc.text = "TRUE" if _is_ntsc(fps) else "FALSE"
    return rate


def _frames_to_timecode(frame: int, fps: float, sep: str = ":") -> str:
    """帧号 → HH:MM:SS<sep>FF 时间码字符串。

    FF 字段用**取整后的 timebase** 计算（与 NLE 时间码口径一致），
    而 frame 本身来自真实帧率换算（`_sec_to_frames`），二者分工不同。
    `sep` 为秒与帧之间的分隔符：非丢帧用 `:`、NTSC 丢帧（DF）用 `;`（NLE 惯例）。
    导出器只在序列起始帧（frame=0）处使用本函数，故对 DF 亦成立。
    """
    tb = int(round(fps))
    total_sec = frame // tb
    sec = total_sec % 60
    minute = (total_sec // 60) % 60
    hour = (total_sec // 3600) % 24
    return f"{hour:02d}:{minute:02d}:{sec:02d}{sep}{frame % tb:02d}"


def _timecode_block(fps: float, frame: int = 0) -> ET.Element:
    """构造 <timecode> 块（部分 NLE 需要）。

    结构：<rate>（timebase/ntsc）+ <string> + <frame> + <displayformat>。
    displayformat 按帧率选：NTSC（23.976/29.97/59.94）用 `DF`（丢帧），
    其余整帧率用 `NDF`（非丢帧）；string 分隔符随之取 `;` / `:`。
    序列起始帧恒为传入的 frame（默认 0）。

    ⚠️ 不确定项：FCP7 实务对 23.976 存在 NDF/DF 两种口径的分歧，本实现按
    "NTSC 一律 DF"的既定约定；真实 NLE 相容性需真人实测确认。
    """
    drop_frame = _is_ntsc(fps)
    tc = ET.Element("timecode")
    tc.append(_rate_block(fps))
    ET.SubElement(tc, "string").text = _frames_to_timecode(
        frame, fps, ";" if drop_frame else ":"
    )
    ET.SubElement(tc, "frame").text = str(frame)
    ET.SubElement(tc, "displayformat").text = "DF" if drop_frame else "NDF"
    return tc


def _sample_characteristics(fps: float, width: int, height: int) -> ET.Element:
    """构造 <samplecharacteristics> 块（序列 format 与源媒体 file 共用）。"""
    sc = ET.Element("samplecharacteristics")
    sc.append(_rate_block(fps))
    ET.SubElement(sc, "width").text = str(width)
    ET.SubElement(sc, "height").text = str(height)
    ET.SubElement(sc, "anamorphic").text = "FALSE"
    ET.SubElement(sc, "pixelaspectratio").text = "square"
    ET.SubElement(sc, "fielddominance").text = "none"
    ET.SubElement(sc, "colordepth").text = "24"
    return sc


def _audio_sample_characteristics(spec: EditSpec) -> ET.Element:
    """构造音频 <samplecharacteristics>（<file> 内 audio 媒体用）。

    位深固定 16（xmeml 常见口径）；采样率取自源元数据，缺失时不写该字段。
    """
    sc = ET.Element("samplecharacteristics")
    ET.SubElement(sc, "depth").text = "16"
    if spec.source.audio_sample_rate:
        ET.SubElement(sc, "samplerate").text = str(spec.source.audio_sample_rate)
    return sc


# --------------------------------------------------------------------------- #
# 路径 → file URL
# --------------------------------------------------------------------------- #
_ABS_WIN = re.compile(r"^[A-Za-z]:/")
_BASENAME = re.compile(r"[\\/]")


def _path_to_file_url(path: str | Path) -> str:
    """把文件路径转成 Premiere / Resolve 能识别的 file URL。

    规则
    ----
    - 反斜杠统一转正斜杠；
    - Windows 绝对路径写成 `file://localhost/C:/path/to/video.mp4`：
      保留盘符后的冒号、盘符前补一个斜杠；
    - 空格、中文等做 percent-encoding（基于 UTF-8），`/` 与 `:` 不做编码；
    - 相对路径先按 cwd 转成绝对路径再编码（相对路径无法生成可用的 file URL）。

    ⚠️ 不确定项：相对路径的"绝对化"依赖运行时 cwd，上游应尽量喂绝对路径。
    """
    raw = str(path).replace("\\", "/")
    if not (raw.startswith("/") or _ABS_WIN.match(raw)):
        raw = os.path.abspath(str(path)).replace("\\", "/")
    # safe="/:" —— 保留路径分隔符与盘符冒号
    encoded = quote(raw, safe="/:")
    if not encoded.startswith("/"):
        encoded = "/" + encoded
    return "file://localhost" + encoded


def _basename(path: str | Path) -> str:
    """跨平台取文件名（同时兼容 / 与 \\ 分隔符）。"""
    return _BASENAME.split(str(path))[-1]


# --------------------------------------------------------------------------- #
# 元素构造
# --------------------------------------------------------------------------- #
def _media_file(
    spec: EditSpec, fps: float, *, file_id: str, media_kind: str
) -> ET.Element:
    """构造 <file> 块（video / audio clipitem 共用）。

    NLE 解析媒体需要：<name> / <pathurl> / <rate> / <duration>，以及 <media> 内
    对应媒体类型的 <samplecharacteristics>（视频含尺寸/像素比，音频含位深/采样率）。
    C 型场景下源素材即整片，故 <file><duration> 等于序列总时长。
    """
    file_el = ET.Element("file", {"id": file_id})
    ET.SubElement(file_el, "name").text = _basename(spec.source.path)
    ET.SubElement(file_el, "pathurl").text = _path_to_file_url(spec.source.path)
    file_el.append(_rate_block(fps))
    ET.SubElement(file_el, "duration").text = str(
        _sec_to_frames(spec.source.duration_sec, fps)
    )
    media = ET.SubElement(file_el, "media")
    if media_kind == "audio":
        audio = ET.SubElement(media, "audio")
        audio.append(_audio_sample_characteristics(spec))
    else:
        video = ET.SubElement(media, "video")
        video.append(
            _sample_characteristics(fps, spec.source.width, spec.source.height)
        )
    return file_el


def _clipitem(
    spec: EditSpec,
    index: int,
    in_f: int,
    out_f: int,
    start_f: int,
    fps: float,
    *,
    kind: str = "video",
) -> ET.Element:
    """构造一个 <clipitem>（kind 为 "video" 或 "audio"）。

    两组时间语义必须分清（这是 xmeml 最容易搞错的地方）：
      - <start>/<end>：clipitem 在**序列时间轴**上的位置，必须**累积**。
        若一律写 0，NLE 导入后所有镜头会叠在第 0 帧上。
      - <in>/<out>：素材**源**内的起止帧。C 型场景下源素材就是原片本身，
        故取 shot.in_sec / shot.out_sec 换算的帧号 —— 这是"还原原片时间线"的正确语义。

    video 与 audio 都挂指向源素材的 <file>：音频挂真实 <file>（含 <pathurl>）是
    "真实 NLE 音频轨"的必要条件，取代了此前无 <file> 的骨架写法。
    """
    if kind == "audio":
        item = ET.Element("clipitem", {"id": f"audio-clipitem-{index}"})
        file_id = "audio-file-1"
    else:
        item = ET.Element("clipitem", {"id": f"clipitem-{index}"})
        file_id = "file-1"
    ET.SubElement(item, "name").text = f"shot_{index}"
    dur = out_f - in_f
    ET.SubElement(item, "duration").text = str(dur)
    item.append(_rate_block(fps))
    ET.SubElement(item, "start").text = str(start_f)
    ET.SubElement(item, "end").text = str(start_f + dur)
    ET.SubElement(item, "in").text = str(in_f)
    ET.SubElement(item, "out").text = str(out_f)
    item.append(_media_file(spec, fps, file_id=file_id, media_kind=kind))
    return item


def _transition_names(cut_type: CutType) -> tuple[str, str, str]:
    """过渡的 (name, effectid, effectcategory)。

    dissolve → Cross Dissolve；fade → Fade。二者都归 Dissolve 类。
    ⚠️ 不确定项：effectid/category 的具体取值需真人实测确认。
    """
    if cut_type == CutType.FADE:
        return ("Fade", "Fade", "Dissolve")
    return ("Cross Dissolve", "Cross Dissolve", "Dissolve")


def _transitionitem(shot, fps: float, cut_f: int) -> ET.Element:
    """构造 <transitionitem>（仅用于 dissolve / fade）。

    时长取 transition.duration_sec 换算帧数，下限 2 帧（理由见下方退化分支）。
    <start>/<end> 同样是序列时间轴位置：转场应**跨骑在剪切点两侧**，
    故 start = 剪切点 - 时长//2，end = start + 时长（左端钳到 0）。
    ⚠️ 不确定项：effectid/effectcategory 取值、以及 NLE 导入时对相邻 clipitem
    的自动裁剪行为，需真人实测确认。
    """
    item = ET.Element("transitionitem")
    name, effectid, category = _transition_names(shot.cut_type)
    src = shot.transition
    dur = _sec_to_frames(src.duration_sec, fps) if src is not None else 0
    if dur < 2:
        # 下限 2 帧：整数帧下"严格跨骑剪切点"（start < 剪切点 < end）最少需要
        # 切点前后各 1 帧。若退化为 1 帧，start 恰好等于剪切点，直接违反导出契约。
        dur = 2
    start_f = max(0, cut_f - dur // 2)
    ET.SubElement(item, "name").text = name
    ET.SubElement(item, "duration").text = str(dur)
    ET.SubElement(item, "start").text = str(start_f)
    ET.SubElement(item, "end").text = str(start_f + dur)
    effect = ET.SubElement(item, "effect")
    ET.SubElement(effect, "name").text = name
    ET.SubElement(effect, "effectid").text = effectid
    ET.SubElement(effect, "effectcategory").text = category
    ET.SubElement(effect, "effecttype").text = "transition"
    ET.SubElement(effect, "mediatype").text = "video"
    ET.SubElement(effect, "wipecode").text = "0"
    ET.SubElement(effect, "wipeaccuracy").text = "100"
    ET.SubElement(effect, "startratio").text = "0"
    ET.SubElement(effect, "endratio").text = "1"
    ET.SubElement(effect, "reverse").text = "FALSE"
    return item


def _fill_video_track(track: ET.Element, spec: EditSpec, fps: float) -> None:
    """按镜头顺序铺一条 video track：clipitem 在序列时间轴上顺序累积排布。

    offset 记录序列时间轴上的当前帧位置，逐镜累加。这条累积逻辑是
    "导入后时间线正确"的必要条件，别退化成逐镜独立写 start=0。
    """
    offset = 0
    for shot in spec.shots:
        in_f = _sec_to_frames(shot.in_sec, fps)
        out_f = _sec_to_frames(shot.out_sec, fps)
        if out_f - in_f < 1:
            out_f = in_f + 1  # 退化保护：镜头不足 1 帧时钳到 1 帧
        dur = out_f - in_f
        # 转场只出现在 dissolve/fade，且必须"在相邻 clipitem 之间"（故 index>0）
        if shot.cut_type in _GRADUAL and shot.index > 0:
            track.append(_transitionitem(shot, fps, cut_f=offset))
        track.append(_clipitem(spec, shot.index, in_f, out_f, offset, fps))
        offset += dur


def _fill_audio_track(track: ET.Element, spec: EditSpec, fps: float) -> None:
    """铺一条**真实** audio track：每个镜头补一个音频 clipitem，累积排布、覆盖整条序列。

    与 video track 逐镜对齐（起点/时长/源内 <in>/<out> 一致），每个音频 clipitem
    都挂真实 <file>（含 <pathurl> 与音频 <samplecharacteristics>）——这是"真实 NLE
    音频轨"的形态，取代了此前无 <file> 的骨架写法。音频侧不写 <transitionitem>
    （转场属视频媒体类型）。
    """
    offset = 0
    for shot in spec.shots:
        in_f = _sec_to_frames(shot.in_sec, fps)
        out_f = _sec_to_frames(shot.out_sec, fps)
        if out_f - in_f < 1:
            out_f = in_f + 1
        dur = out_f - in_f
        track.append(
            _clipitem(spec, shot.index, in_f, out_f, offset, fps, kind="audio")
        )
        offset += dur


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def to_fcp7_xml(spec: EditSpec, fps_override: float | None = None) -> str:
    """把 EditSpec 渲染成 FCP7 XML 字符串（裸 xmeml，无 XML namespace）。"""
    fps = _resolve_fps(spec, fps_override)

    root = ET.Element("xmeml", {"version": "4"})
    seq = ET.SubElement(root, "sequence", {"id": "rve-sequence"})
    ET.SubElement(seq, "name").text = spec.spec_id
    total_frames = _sec_to_frames(spec.source.duration_sec, fps)
    ET.SubElement(seq, "duration").text = str(total_frames)
    seq.append(_rate_block(fps))
    # <timecode>：部分 NLE 解析序列时需要（起始帧 + NDF）
    seq.append(_timecode_block(fps))

    media = ET.SubElement(seq, "media")
    video = ET.SubElement(media, "video")
    # 序列视频规格（宽/高/帧率），取自源
    video_format = ET.SubElement(video, "format")
    video_format.append(
        _sample_characteristics(fps, spec.source.width, spec.source.height)
    )
    _fill_video_track(ET.SubElement(video, "track"), spec, fps)

    # audio：仅在源含音频时生成，避免给无声片硬塞空音轨
    if spec.source.has_audio:
        audio = ET.SubElement(media, "audio")
        _fill_audio_track(ET.SubElement(audio, "track"), spec, fps)

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    return body.decode("utf-8")


def write_fcp7_xml(
    spec: EditSpec, out_path: str | Path, fps_override: float | None = None
) -> Path:
    """渲染 FCP7 XML 并落盘，返回写入路径。"""
    xml = to_fcp7_xml(spec, fps_override)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")
    return path
