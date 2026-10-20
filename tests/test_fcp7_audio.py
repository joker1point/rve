"""FCP7 导出器「贴近真实 NLE」的结构断言 —— 纯标准库，不依赖 opentimelineio。

补齐三项真实 NLE 一定会写、而早期骨架版缺失的元素：
  ① 序列级 <timecode> 的 displayformat：NTSC（丢帧）用 `DF`、其余用 `NDF`；
  ② 每个 <file> 的 <duration> 与 <samplecharacteristics>（width/height/pixelaspectratio）；
  ⑤ audio track 的真实 <file>（含 <pathurl>），不再是"无 <file> 的骨架"。

同时锁两条回归契约：
  - 加音频轨**不得**扰动 video 轨的"连续铺满"契约；
  - 音频轨随 `source.has_audio` 出现 / 消失。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from rve.exporters.fcp7 import _path_to_file_url, to_fcp7_xml
from rve.schema.editspec import EditSpec, Shot, VideoSource

FIXTURE = Path(__file__).parent / "fixtures" / "sample_editspec.json"


# --------------------------------------------------------------------------- #
# 夹具与工具
# --------------------------------------------------------------------------- #
def _load_spec() -> EditSpec:
    """读取冻结夹具（由别的 agent 拥有，这里只读）。12.34s @30fps，has_audio=True。"""
    return EditSpec.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


def _make_spec(fps: float = 30.0, duration: float = 10.0, has_audio: bool = True) -> EditSpec:
    """最小合法单镜 spec（用于帧率 / 音频开关等场景）。"""
    source = VideoSource(
        path="C:\\videos\\a b.mp4",
        duration_sec=duration,
        fps=fps,
        width=1920,
        height=1080,
        audio_sample_rate=48000,
        has_audio=has_audio,
    )
    return EditSpec(
        spec_id="t",
        source=source,
        shots=[Shot(index=0, in_sec=0.0, out_sec=duration)],
    )


def _video_items(root: ET.Element) -> list[ET.Element]:
    return root.findall("sequence/media/video/track/clipitem")


def _audio_items(root: ET.Element) -> list[ET.Element]:
    return root.findall("sequence/media/audio/track/clipitem")


def _spans(items: list[ET.Element]) -> list[tuple[int, int]]:
    return [(int(ci.find("start").text), int(ci.find("end").text)) for ci in items]


# --------------------------------------------------------------------------- #
# ① 序列 timecode：NDF 与 NTSC drop-frame
# --------------------------------------------------------------------------- #
def test_sequence_timecode_ndf_for_non_ntsc() -> None:
    """非 NTSC（整帧率，如 30fps）：displayformat = NDF，string 用 `:`。"""
    root = ET.fromstring(to_fcp7_xml(_make_spec(fps=30.0, duration=10.0)))
    tc = root.find("sequence/timecode")
    assert tc is not None
    assert int(tc.find("frame").text) == 0
    assert tc.find("displayformat").text == "NDF"
    assert int(tc.find("rate/timebase").text) == 30
    assert tc.find("rate/ntsc").text == "FALSE"
    assert tc.find("string").text == "00:00:00:00"


def test_sequence_timecode_dropframe_for_29_97() -> None:
    """NTSC 29.97 → drop-frame：displayformat = DF，string 用 `;`，timebase=30/ntsc=TRUE。"""
    root = ET.fromstring(to_fcp7_xml(_make_spec(fps=29.97, duration=10.0)))
    tc = root.find("sequence/timecode")
    assert tc is not None
    assert int(tc.find("frame").text) == 0
    assert tc.find("displayformat").text == "DF"
    assert int(tc.find("rate/timebase").text) == round(29.97) == 30
    assert tc.find("rate/ntsc").text == "TRUE"
    assert tc.find("string").text == "00:00:00;00"


def test_sequence_timecode_dropframe_for_23_976_and_59_94() -> None:
    """其余 NTSC 帧率（23.976 / 59.94）同样按 DF 处理，timebase 为取整整数。"""
    root = ET.fromstring(to_fcp7_xml(_make_spec(fps=23.976, duration=10.0)))
    tc = root.find("sequence/timecode")
    assert tc.find("displayformat").text == "DF"
    assert int(tc.find("rate/timebase").text) == 24
    assert tc.find("rate/ntsc").text == "TRUE"

    root = ET.fromstring(to_fcp7_xml(_make_spec(fps=59.94, duration=10.0)))
    tc = root.find("sequence/timecode")
    assert tc.find("displayformat").text == "DF"
    assert int(tc.find("rate/timebase").text) == 60
    assert tc.find("rate/ntsc").text == "TRUE"


# --------------------------------------------------------------------------- #
# ② <file> 的 <duration> 与 <samplecharacteristics>
# --------------------------------------------------------------------------- #
def test_video_file_duration_equals_sequence_total() -> None:
    spec = _load_spec()
    root = ET.fromstring(to_fcp7_xml(spec))
    total = int(root.find("sequence/duration").text)
    file_el = _video_items(root)[0].find("file")
    assert file_el is not None
    assert int(file_el.find("duration").text) == total
    # <file> 的 rate 与序列一致
    assert int(file_el.find("rate/timebase").text) == 30


def test_video_file_samplecharacteristics_matches_source() -> None:
    spec = _load_spec()
    root = ET.fromstring(to_fcp7_xml(spec))
    sc = _video_items(root)[0].find("file/media/video/samplecharacteristics")
    assert sc is not None
    assert int(sc.find("width").text) == spec.source.width == 1080
    assert int(sc.find("height").text) == spec.source.height == 1920
    assert sc.find("pixelaspectratio").text == "square"
    assert int(sc.find("rate/timebase").text) == 30


def test_every_video_clipitem_file_has_duration_and_samplecharacteristics() -> None:
    """每个 clipitem 的 <file> 都必须带 duration + samplecharacteristics（非仅首个）。"""
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    items = _video_items(root)
    assert len(items) == 5
    for ci in items:
        assert ci.find("file/duration") is not None
        assert ci.find("file/media/video/samplecharacteristics/width") is not None
        assert ci.find("file/media/video/samplecharacteristics/height") is not None
        assert ci.find("file/media/video/samplecharacteristics/pixelaspectratio") is not None


# --------------------------------------------------------------------------- #
# ⑤ 真实音频轨
# --------------------------------------------------------------------------- #
def test_audio_track_present_with_real_file() -> None:
    """has_audio=True：音频轨存在，每个音频 clipitem 挂真实 <file>（含 pathurl）。"""
    spec = _load_spec()
    root = ET.fromstring(to_fcp7_xml(spec))
    items = _audio_items(root)
    assert len(items) == len(spec.shots) == 5
    total = int(root.find("sequence/duration").text)
    for ci in items:
        file_el = ci.find("file")
        assert file_el is not None, "音频 clipitem 必须带真实 <file>"
        assert file_el.find("pathurl") is not None
        assert file_el.find("pathurl").text == _path_to_file_url(spec.source.path)
        assert file_el.find("name").text == "sample.mp4"
        assert int(file_el.find("duration").text) == total
        # 音频 <file> 内是 audio 媒体类型，带音频 samplecharacteristics
        sc = file_el.find("media/audio/samplecharacteristics")
        assert sc is not None
        assert sc.find("samplerate").text == str(spec.source.audio_sample_rate)
        assert sc.find("depth").text == "16"


def test_audio_clipitems_cover_whole_sequence() -> None:
    """音频 clipitem 必须自 0 起、连续、末点等于序列总时长（覆盖整条序列）。"""
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    spans = _spans(_audio_items(root))
    total = int(root.find("sequence/duration").text)
    assert spans[0][0] == 0
    assert spans[-1][1] == total
    for prev, cur in zip(spans, spans[1:]):
        assert cur[0] == prev[1], f"音频 clipitem 不连续: {prev} -> {cur}"


def test_audio_track_absent_when_no_audio() -> None:
    """has_audio=False：不得写 <audio> 轨（不给无声片硬塞空音轨）。"""
    root = ET.fromstring(to_fcp7_xml(_make_spec(has_audio=False)))
    assert root.find("sequence/media/audio") is None


def test_audio_track_does_not_disturb_video_contiguity() -> None:
    """回归锁：加音频轨/开关 has_audio 不得改变 video 轨的连续铺满契约。

    对同一份夹具仅翻转 has_audio，video 轨的所有 [start, end) 必须逐帧一致。
    """
    on = _load_spec()  # has_audio=True
    root_on = ET.fromstring(to_fcp7_xml(on))

    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["source"]["has_audio"] = False  # 仅改这一个开关，其余字段不变
    off = EditSpec.model_validate(data)
    root_off = ET.fromstring(to_fcp7_xml(off))

    assert root_off.find("sequence/media/audio") is None  # 音频轨按开关消失

    # video 轨逐帧一致
    assert _spans(_video_items(root_on)) == _spans(_video_items(root_off))
    spans = _spans(_video_items(root_off))
    total = int(root_off.find("sequence/duration").text)
    assert spans[0][0] == 0
    assert spans[-1][1] == total
    for prev, cur in zip(spans, spans[1:]):
        assert cur[0] == prev[1], f"video 轨被扰动: {prev} -> {cur}"
