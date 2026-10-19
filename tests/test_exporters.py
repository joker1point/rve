"""导出层测试 —— 结构断言（不做真实 NLE 导入，那是 Wave 2 的人工环节）。

- FCP7 相关测试**纯标准库**，不依赖 opentimelineio；
- OTIO 相关测试用 `pytest.importorskip("opentimelineio")` 包裹，未装即 skip；
- export_timeline / render_report 通过 sys.modules 注入 `rve.workspace` stub 来测，
  这样在 workspace 模块尚未就位时也能验证编排逻辑。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from rve.exporters.fcp7 import _path_to_file_url, to_fcp7_xml, write_fcp7_xml
from rve.pipeline.export import export_timeline
from rve.pipeline.report import render_report
from rve.schema.editspec import EditSpec, Shot, VideoSource
from rve.schema.enums import ExportFormat

FIXTURE = Path(__file__).parent / "fixtures" / "sample_editspec.json"


# --------------------------------------------------------------------------- #
# 夹具与工具
# --------------------------------------------------------------------------- #
def _load_spec() -> EditSpec:
    """从冻结夹具构造 EditSpec（夹具由别的 agent 拥有，这里只读）。"""
    return EditSpec.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


def _make_spec(fps: float = 30.0, duration: float = 10.0, has_audio: bool = True) -> EditSpec:
    """构造一个最小合法单镜 spec（用于帧率/音频等场景）。"""
    source = VideoSource(
        path="C:\\videos\\a b.mp4",
        duration_sec=duration,
        fps=fps,
        width=1920,
        height=1080,
        has_audio=has_audio,
    )
    return EditSpec(
        spec_id="t",
        source=source,
        shots=[Shot(index=0, in_sec=0.0, out_sec=duration)],
    )


def _rate_of(root: ET.Element) -> tuple[int, str]:
    """读取 sequence 的 (timebase, ntsc)。"""
    rate = root.find("sequence/rate")
    assert rate is not None
    return int(rate.find("timebase").text), rate.find("ntsc").text


def _video_items(root: ET.Element) -> list[ET.Element]:
    """video track 内的 clipitem（不含音频轨）。"""
    track = root.find("sequence/media/video/track")
    assert track is not None
    return track.findall("clipitem")


def _video_transitions(root: ET.Element) -> list[ET.Element]:
    """video track 内的 transitionitem。"""
    return root.findall("sequence/media/video/track/transitionitem")


def _install_workspace(monkeypatch, spec: EditSpec) -> None:
    """把 rve.workspace 做成 stub 注入 sys.modules，供延迟导入命中。"""
    mod = types.ModuleType("rve.workspace")
    mod.read_spec = lambda spec_id: spec  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rve.workspace", mod)


# --------------------------------------------------------------------------- #
# FCP7：结构与时间码
# --------------------------------------------------------------------------- #
def test_fcp7_root_and_sequence_shape() -> None:
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    assert root.tag == "xmeml"  # 裸 xmeml，无 namespace
    assert root.get("version") == "4"
    seq = root.find("sequence")
    assert seq is not None and seq.get("id") == "rve-sequence"
    assert seq.find("name") is not None
    assert seq.find("media/video/track") is not None


def test_clipitem_count_equals_shot_count() -> None:
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    items = _video_items(root)
    assert len(items) == 5
    assert [i.get("id") for i in items] == [f"clipitem-{n}" for n in range(5)]
    assert [i.find("name").text for i in items] == [f"shot_{n}" for n in range(5)]


def test_sequence_duration_equals_frame_sum() -> None:
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    total = int(root.find("sequence/duration").text)
    durations = [int(ci.find("duration").text) for ci in _video_items(root)]
    # 12.34s @30fps → round(370.2) == 370
    assert total == round(12.34 * 30) == 370
    assert sum(durations) == total
    # <start>/<end> 是序列时间轴上的位置，必须累积。本夹具是"整条原片重排"，
    # 故累积起点恰好与源内 <in> 一致。
    starts = [int(ci.find("start").text) for ci in _video_items(root)]
    ends = [int(ci.find("end").text) for ci in _video_items(root)]
    assert starts == [0, 69, 151, 180, 282]
    assert ends == [69, 151, 180, 282, 370]


def test_clipitems_laid_out_sequentially() -> None:
    """序列时间轴上必须首尾相接、单调不回退。

    若退化成逐镜写 start=0，NLE 导入后所有镜头会叠在第 0 帧——这是本导出器
    最危险的一类回归，故单独锁一条测试。
    """
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    spans = [
        (int(ci.find("start").text), int(ci.find("end").text))
        for ci in _video_items(root)
    ]
    for prev, cur in zip(spans, spans[1:]):
        assert cur[0] >= prev[1], f"clipitem 重叠或回退: {prev} -> {cur}"
    assert spans[0][0] == 0
    assert spans[-1][1] == int(root.find("sequence/duration").text)


def test_in_out_are_source_frames() -> None:
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    expected = [(0, 69), (69, 151), (151, 180), (180, 282), (282, 370)]
    got = [(int(ci.find("in").text), int(ci.find("out").text)) for ci in _video_items(root)]
    assert got == expected


def test_rate_non_ntsc_matches_fps() -> None:
    root = ET.fromstring(to_fcp7_xml(_load_spec()))  # 30fps
    timebase, ntsc = _rate_of(root)
    assert (timebase, ntsc) == (30, "FALSE")


def test_ntsc_true_for_29_97() -> None:
    root = ET.fromstring(to_fcp7_xml(_make_spec(fps=29.97, duration=10.0)))
    timebase, ntsc = _rate_of(root)
    assert (timebase, ntsc) == (30, "TRUE")
    # 帧号必须用真实帧率换算，而非取整后的 30
    assert int(root.find("sequence/duration").text) == round(10.0 * 29.97) == 300
    # clipitem 内的 rate 块与序列一致
    ci_rate = _video_items(root)[0].find("rate")
    assert ci_rate.find("ntsc").text == "TRUE"
    assert int(ci_rate.find("timebase").text) == 30


def test_ntsc_true_for_23_976() -> None:
    root = ET.fromstring(to_fcp7_xml(_make_spec(fps=23.976, duration=10.0)))
    assert _rate_of(root) == (24, "TRUE")
    assert int(root.find("sequence/duration").text) == round(10.0 * 23.976) == 240


def test_fps_override_takes_priority() -> None:
    # 源 30fps，override 24fps → 采用 24 且非 NTSC
    root = ET.fromstring(to_fcp7_xml(_load_spec(), fps_override=24.0))
    assert _rate_of(root) == (24, "FALSE")
    assert int(root.find("sequence/duration").text) == round(12.34 * 24) == 296


def test_transitionitem_only_at_dissolve_fade() -> None:
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    transitions = _video_transitions(root)
    # 夹具仅 shots[1]=dissolve、shots[4]=fade
    assert len(transitions) == 2
    assert [t.find("name").text for t in transitions] == ["Cross Dissolve", "Fade"]
    assert [int(t.find("duration").text) for t in transitions] == [
        round(0.4 * 30),
        round(0.6 * 30),
    ]
    # 转场跨骑剪切点：shots[1] 剪切点在帧 69、时长 12 → [63, 75)
    #                shots[4] 剪切点在帧 282、时长 18 → [273, 291)
    spans = [(int(t.find("start").text), int(t.find("end").text)) for t in transitions]
    assert spans == [(63, 75), (273, 291)]
    # 每个 transitionitem 都带 effect 块
    for t in transitions:
        assert t.find("effect/effectid") is not None

    # 硬切处不插 transitionitem：shots[0]/[2]/[3] 为 hard
    item_ids = [ci.get("id") for ci in _video_items(root)]
    assert item_ids == [f"clipitem-{n}" for n in range(5)]


def test_file_element_name_and_pathurl() -> None:
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    file_el = _video_items(root)[0].find("file")
    assert file_el is not None and file_el.get("id") == "file-1"
    assert file_el.find("name").text == "sample.mp4"  # fixtures/sample.mp4 → 文件名
    assert file_el.find("pathurl").text.startswith("file://localhost/")


def test_audio_track_present_when_source_has_audio() -> None:
    root = ET.fromstring(to_fcp7_xml(_load_spec()))  # 夹具 has_audio=True
    track = root.find("sequence/media/audio/track")
    assert track is not None
    items = track.findall("clipitem")
    assert len(items) == 5
    # 音频轨同样按累积位置铺排
    assert [int(ci.find("start").text) for ci in items] == [0, 69, 151, 180, 282]
    # 音频 clipitem 为骨架，不挂 <file>
    for ci in items:
        assert ci.find("file") is None


def test_audio_track_absent_when_no_audio() -> None:
    root = ET.fromstring(to_fcp7_xml(_make_spec(has_audio=False)))
    assert root.find("sequence/media/audio") is None


def test_sequence_timecode_block_present_and_correct() -> None:
    """<sequence><timecode>：部分 NLE 需要；起始帧为 0、displayformat 固定 NDF。

    资历背景：缺 <timecode> 时某些 NLE 拒绝解析序列，故单独锁一条回归。
    """
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    tc = root.find("sequence/timecode")
    assert tc is not None
    assert int(tc.find("frame").text) == 0
    assert tc.find("displayformat").text == "NDF"
    assert int(tc.find("rate/timebase").text) == 30
    assert tc.find("rate/ntsc").text == "FALSE"
    # string 与 frame 同源：起始帧的时间码字符串
    assert tc.find("string").text == "00:00:00:00"


def test_sequence_samplecharacteristics_matches_source() -> None:
    """序列 video format 的 samplecharacteristics 必须等于源尺寸/帧率。"""
    spec = _load_spec()
    root = ET.fromstring(to_fcp7_xml(spec))
    sc = root.find("sequence/media/video/format/samplecharacteristics")
    assert sc is not None
    assert int(sc.find("width").text) == spec.source.width
    assert int(sc.find("height").text) == spec.source.height
    assert int(sc.find("rate/timebase").text) == 30
    assert sc.find("rate/ntsc").text == "FALSE"


def test_file_duration_equals_sequence_total() -> None:
    """<file><duration> 必须等于序列总时长（C 型：源素材即整片）。"""
    root = ET.fromstring(to_fcp7_xml(_load_spec()))
    total = int(root.find("sequence/duration").text)
    file_el = _video_items(root)[0].find("file")
    assert file_el is not None
    assert int(file_el.find("duration").text) == total


def test_file_samplecharacteristics_matches_source() -> None:
    """<file> 内的 samplecharacteristics：让 Premiere / Resolve 正确解析媒体。"""
    spec = _load_spec()
    root = ET.fromstring(to_fcp7_xml(spec))
    file_el = _video_items(root)[0].find("file")
    assert file_el is not None
    sc = file_el.find("media/video/samplecharacteristics")
    assert sc is not None
    assert int(sc.find("width").text) == spec.source.width
    assert int(sc.find("height").text) == spec.source.height


# --------------------------------------------------------------------------- #
# FCP7：file URL 编码
# --------------------------------------------------------------------------- #
def test_path_to_file_url_windows() -> None:
    got = _path_to_file_url(r"C:\Users\biren\My Videos\样片 01.mp4")
    assert got == "file://localhost/C:/Users/biren/My%20Videos/%E6%A0%B7%E7%89%87%2001.mp4"
    assert "\\" not in got


def test_path_to_file_url_windows_forward_slashes() -> None:
    assert _path_to_file_url("C:/videos/a.mp4") == "file://localhost/C:/videos/a.mp4"


def test_path_to_file_url_posix_and_relative() -> None:
    assert _path_to_file_url("/tmp/a b.mp4") == "file://localhost/tmp/a%20b.mp4"
    # 相对路径先绝对化，结果以 file://localhost/ 开头
    assert _path_to_file_url("rel/clip 1.mp4").startswith("file://localhost/")


# --------------------------------------------------------------------------- #
# FCP7：落盘
# --------------------------------------------------------------------------- #
def test_write_fcp7_xml_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "sample.fcp7xml"
    written = write_fcp7_xml(_load_spec(), out)
    assert written == out and out.exists()
    root = ET.fromstring(out.read_text(encoding="utf-8"))
    assert root.tag == "xmeml"


# --------------------------------------------------------------------------- #
# 导入卫生：无 otio 也应可用
# --------------------------------------------------------------------------- #
def test_exporters_package_importable() -> None:
    import rve.exporters as exporters

    assert hasattr(exporters, "to_fcp7_xml")


def test_otio_module_imports_without_otio() -> None:
    """otio_export 顶层不得 import otio，否则此测试会在未装 otio 时报错。"""
    mod = importlib.import_module("rve.exporters.otio_export")
    assert hasattr(mod, "to_otio") and hasattr(mod, "write_otio")


# --------------------------------------------------------------------------- #
# 编排层（异步）
# --------------------------------------------------------------------------- #
def test_export_timeline_fcp7(monkeypatch, tmp_path: Path) -> None:
    spec = _load_spec()
    _install_workspace(monkeypatch, spec)
    out = tmp_path / "out.fcp7xml"

    result = asyncio.run(
        export_timeline(spec.spec_id, ExportFormat.FCP7XML, str(out))
    )
    assert result.receipt.ok
    assert result.format == "fcp7xml"
    assert Path(result.out_path).exists()
    ET.fromstring(Path(result.out_path).read_text(encoding="utf-8"))


def test_export_timeline_edl(monkeypatch, tmp_path: Path) -> None:
    """EDL（CMX3600，纯标准库）：产物存在，事件数 == 镜头数。"""
    spec = _load_spec()
    _install_workspace(monkeypatch, spec)
    out = tmp_path / "out.edl"

    result = asyncio.run(
        export_timeline(spec.spec_id, ExportFormat.EDL, str(out))
    )
    assert result.receipt.ok
    assert result.format == "edl"
    path = Path(result.out_path)
    assert path.exists()

    text = path.read_text(encoding="utf-8")
    assert text.startswith("TITLE:")
    assert "FCM: NON-DROP FRAME" in text
    # 事件行形如 `001  AX       V     C  ...`；其数量必须等于镜头数
    events = [ln for ln in text.splitlines() if re.match(r"^\d{3}\s", ln)]
    assert len(events) == len(spec.shots) == 5


def test_render_report_html(monkeypatch, tmp_path: Path) -> None:
    spec = _load_spec()
    _install_workspace(monkeypatch, spec)
    monkeypatch.chdir(tmp_path)

    result = asyncio.run(render_report(spec.spec_id))
    report = Path(result.out_path)
    assert report.exists() and result.receipt.ok
    text = report.read_text(encoding="utf-8")
    assert spec.spec_id in text
    assert "分镜报告" in text
    assert "镜头表" in text


# --------------------------------------------------------------------------- #
# OTIO（未装即 skip）
# --------------------------------------------------------------------------- #
def test_to_otio_structure() -> None:
    otio = pytest.importorskip("opentimelineio")
    from rve.exporters.otio_export import to_otio

    spec = _load_spec()
    timeline = to_otio(spec)
    assert isinstance(timeline, otio.schema.Timeline)
    # 视频 + 音频两条轨
    assert len(timeline.tracks) == 2
    video = timeline.tracks[0]
    clips = [c for c in video if isinstance(c, otio.schema.Clip)]
    assert len(clips) == 5
    assert clips[0].source_range.start_time.value == pytest.approx(0.0, abs=1e-6)
    # 每个 clip 必须显式挂回源素材（ExternalReference + target_url）：
    # 否则 OTIO 默认给 MissingReference，导出的 .otio 在 NLE 里重链不回媒体。
    for c in clips:
        assert isinstance(c.media_reference, otio.schema.ExternalReference)
        assert c.media_reference.target_url == spec.source.path


def test_write_otio(tmp_path: Path) -> None:
    pytest.importorskip("opentimelineio")
    from rve.exporters.otio_export import write_otio

    out = tmp_path / "sample.otio"
    written = write_otio(_load_spec(), out)
    assert written == out and out.exists()


def test_export_timeline_otio(monkeypatch, tmp_path: Path) -> None:
    """编排层 OTIO 导出：产物存在、成功（未装 otio 即 skip）。"""
    pytest.importorskip("opentimelineio")
    spec = _load_spec()
    _install_workspace(monkeypatch, spec)
    out = tmp_path / "out.otio"

    result = asyncio.run(
        export_timeline(spec.spec_id, ExportFormat.OTIO, str(out))
    )
    assert result.receipt.ok
    assert result.format == "otio"
    assert Path(result.out_path).exists()
