"""模拟真人导入相关脚本的测试（scripts/make_nle_ground_truth.py + scripts/simulate_nle_import.py）。

覆盖：
- 真值脚本的帧级推导（纯计算，不必跑 ffmpeg）；
- 素材准备与真值落盘（需要 ffmpeg，缺则显式 skip）；
- 独立解析器的 NLE 侧基础能力：帧率换算、file URL 还原、坏输入必须报错；
- 独立解析器的**判错能力**：素材离线、时间线有空洞，都必须判 FAIL（不能只会在 happy path 上通过）。
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_module(name: str):
    """按文件路径加载 scripts/ 下的模块（它们不是包内模块，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass 在装饰时需要按模块名回查 sys.modules，必须先注册
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


maker = _load_module("make_nle_ground_truth")
simulator = _load_module("simulate_nle_import")

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="需要 ffmpeg 合成素材（本机未安装 ffmpeg）"
)


# --------------------------------------------------------------------------- #
# 真值推导（纯计算）
# --------------------------------------------------------------------------- #
def test_ground_truth_frame_derivation() -> None:
    """真值必须由帧数精确推出，不能是拍脑袋的小数。"""
    truth = maker._build_truth()
    assert truth["fps"] == 30
    assert truth["total_frames"] == 216
    assert truth["total_duration_sec"] == pytest.approx(7.2)
    # 3 处硬切：red|bars、bars|green、green|blue
    assert truth["hard_cuts_sec"] == [2.0, 3.5, 4.7]
    # 2 段转场窗：溶解 [5.2,5.7)、淡出 [5.7,6.2)
    spans = [t["span_sec"] for t in truth["transitions"]]
    assert spans == [[5.2, 5.7], [5.7, 6.2]]
    assert [t["type"] for t in truth["transitions"]] == ["dissolve", "fade"]
    # 纯硬切切出「硬切数+1」镜，两处渐变各可能额外切一刀 → 4~6
    assert truth["shot_count_min"] == 4
    assert truth["shot_count_max"] == 6


# --------------------------------------------------------------------------- #
# 素材准备
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_maker_writes_material_and_truth(tmp_path: Path) -> None:
    """真值脚本要真的产出素材与真值文件，且素材时长与设计值一致。"""
    rc = maker.main(["--out", str(tmp_path)])
    assert rc == 0
    video = tmp_path / "nle_fixture.mp4"
    truth_path = tmp_path / "ground_truth.json"
    assert video.is_file() and video.stat().st_size > 0
    assert truth_path.is_file()
    import json

    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    assert truth["total_duration_sec"] == pytest.approx(7.2)


# --------------------------------------------------------------------------- #
# NLE 侧基础能力
# --------------------------------------------------------------------------- #
def test_fps_from_rate_handles_ntsc() -> None:
    """NTSC 整数帧率要还原成真实帧率（30 ↔ 29.97）。"""
    assert simulator.fps_from_rate(30, False) == pytest.approx(30.0)
    assert simulator.fps_from_rate(24, False) == pytest.approx(24.0)
    assert simulator.fps_from_rate(30, True) == pytest.approx(29.97002997, rel=1e-6)
    assert simulator.fps_from_rate(24, True) == pytest.approx(23.97602397, rel=1e-6)
    assert simulator.fps_from_rate(0, False) == 0.0


def test_file_url_to_path() -> None:
    """file URL 要能还原成本地路径（Windows 与 POSIX 都要）。"""
    assert simulator.file_url_to_path("file://localhost/C:/dir/a.mp4") == "C:/dir/a.mp4"
    assert simulator.file_url_to_path("file:///home/u/a.mp4") == "/home/u/a.mp4"
    # 中文与空格要还原成未编码的路径
    assert simulator.file_url_to_path("file://localhost/C:/dir/a%20b.mp4") == "C:/dir/a b.mp4"
    # 非 file URL 一律返回 None（不猜）
    assert simulator.file_url_to_path("https://example.com/a.mp4") is None
    assert simulator.file_url_to_path("") is None


def test_load_timeline_rejects_bad_input(tmp_path: Path) -> None:
    """坏输入必须显式报错，绝不返回空壳时间线。"""
    with pytest.raises(FileNotFoundError):
        simulator.load_timeline(tmp_path / "nope.xml")

    bad_root = tmp_path / "bad_root.xml"
    bad_root.write_text("<not_xmeml/>", encoding="utf-8")
    with pytest.raises(ValueError):
        simulator.load_timeline(bad_root)

    no_seq = tmp_path / "no_seq.xml"
    no_seq.write_text("<xmeml version='4'></xmeml>", encoding="utf-8")
    with pytest.raises(ValueError):
        simulator.load_timeline(no_seq)

    broken = tmp_path / "broken.xml"
    broken.write_text("<xmeml>", encoding="utf-8")
    with pytest.raises(ValueError):
        simulator.load_timeline(broken)


# --------------------------------------------------------------------------- #
# 判错能力（不能只会在 happy path 上通过）
# --------------------------------------------------------------------------- #
_MINIMAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<xmeml version="4">
  <sequence id="s1">
    <name>demo</name>
    <duration>60</duration>
    <rate><timebase>30</timebase><ntsc>FALSE</ntsc></rate>
    <media>
      <video>
        <track>
          <clipitem id="c1">
            <name>shot_0</name>
            <duration>30</duration>
            <rate><timebase>30</timebase><ntsc>FALSE</ntsc></rate>
            <start>0</start><end>30</end><in>0</in><out>30</out>
            <file id="f1">
              <name>src.mp4</name>
              <pathurl>file://localhost/{path}</pathurl>
              <rate><timebase>30</timebase><ntsc>FALSE</ntsc></rate>
              <duration>60</duration>
            </file>
          </clipitem>
          <clipitem id="c2">
            <name>shot_1</name>
            <duration>30</duration>
            <rate><timebase>30</timebase><ntsc>FALSE</ntsc></rate>
            <start>40</start><end>70</end><in>30</in><out>60</out>
            <file id="f1">
              <name>src.mp4</name>
              <pathurl>file://localhost/{path}</pathurl>
              <rate><timebase>30</timebase><ntsc>FALSE</ntsc></rate>
              <duration>60</duration>
            </file>
          </clipitem>
        </track>
      </video>
    </media>
  </sequence>
</xmeml>
"""


def _write_minimal_xml(tmp_path: Path, media: Path) -> Path:
    path = tmp_path / "timeline.xml"
    url_path = str(media).replace("\\", "/")
    path.write_text(_MINIMAL_XML.format(path=url_path), encoding="utf-8")
    return path


def test_simulator_flags_gap_between_clips(tmp_path: Path) -> None:
    """两片段之间有空洞（30→40）时，必须判 FAIL。"""
    media = tmp_path / "src.mp4"
    media.write_bytes(b"fake")
    xml = _write_minimal_xml(tmp_path, media)

    tl = simulator.load_timeline(xml)
    checks = simulator.structural_checks(tl)
    by_name = {c.name: c for c in checks}
    assert by_name["片段首尾相接"].passed is False
    assert "空洞" in by_name["片段首尾相接"].actual
    assert by_name["序列时长自洽"].passed is False


def test_simulator_flags_offline_media(tmp_path: Path) -> None:
    """素材文件不存在时，必须判 FAIL（媒体离线是导入最常见的失败）。"""
    xml = _write_minimal_xml(tmp_path, tmp_path / "not_there.mp4")

    tl = simulator.load_timeline(xml)
    checks = simulator.structural_checks(tl)
    by_name = {c.name: c for c in checks}
    assert by_name["素材链接可用"].passed is False
    assert "not_there.mp4" in by_name["素材链接可用"].actual


def test_simulator_flags_missing_transition_staddle(tmp_path: Path) -> None:
    """转场不跨骑任何剪切点时，必须判 FAIL。"""
    media = tmp_path / "src.mp4"
    media.write_bytes(b"fake")
    xml = _write_minimal_xml(tmp_path, media)
    text = xml.read_text(encoding="utf-8")
    # 塞一个不跨骑剪切点的转场（剪切点在 30，转场却在 50~60）
    text = text.replace(
        "        </track>",
        """          <transitionitem>
            <name>Cross Dissolve</name>
            <duration>10</duration><start>50</start><end>60</end>
            <effect><name>Cross Dissolve</name><effectid>Cross Dissolve</effectid></effect>
          </transitionitem>
        </track>""",
    )
    xml.write_text(text, encoding="utf-8")

    tl = simulator.load_timeline(xml)
    checks = simulator.structural_checks(tl)
    by_name = {c.name: c for c in checks}
    assert by_name["转场跨骑剪切点"].passed is False
