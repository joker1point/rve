"""独立校验器（scripts/independent_verify.py）的测试。

设计要点（与交付要求同源）：
- **不 import rve 的导出器**。真实产物通过 **CLI 子进程** 现生成
  （``python -m rve.cli analyze tests/data/synth_cut.mp4``），校验器再读回，
  与被验代码库彻底解耦；
- 校验器以 ``importlib`` 从文件加载（它本身只依赖标准库，不 import rve.*）；
- 用手工构造的、**故意坏掉**的 XML（制造空洞 / 重叠）断言校验器**必须失败**。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
SYNTH = REPO_ROOT / "tests" / "data" / "synth_cut.mp4"
VERIFIER_PATH = REPO_ROOT / "scripts" / "independent_verify.py"


# --------------------------------------------------------------------------- #
# 加载被测校验器 + 工具
# --------------------------------------------------------------------------- #
def _load_verifier():
    """从文件路径加载校验器模块（不经由任何 rve 包）。"""
    spec = importlib.util.spec_from_file_location("independent_verify", VERIFIER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_cli(args: list[str], env: dict[str, str]):
    """以子进程方式跑 rve CLI（src 通过 PYTHONPATH 注入，不依赖已安装）。"""
    return subprocess.run(
        [sys.executable, "-m", "rve.cli", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )


def _child_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env.update(overrides)
    return env


# --------------------------------------------------------------------------- #
# 手工夹具：一份与 spec 匹配的最小 XML（两镜、含音频轨、从 0 连续铺满）
# --------------------------------------------------------------------------- #
_SPEC = {
    "schema_version": "0.1",
    "spec_id": "iv-fixture",
    "source": {
        "path": "C:\\videos\\a.mp4",
        "duration_sec": 10.0,
        "fps": 30.0,
        "width": 1920,
        "height": 1080,
        "has_audio": True,
    },
    "shots": [
        {"index": 0, "in_sec": 0.0, "out_sec": 5.0, "cut_type": "hard"},
        {"index": 1, "in_sec": 5.0, "out_sec": 10.0, "cut_type": "hard"},
    ],
}


def _xml(clip1_start: int = 150, clip1_end: int = 300, seq_duration: int = 300) -> str:
    """构造一份 xmeml。默认完全合法；改 clip1_start/end 即可造出空洞或重叠。

    帧率 30fps：镜 0 = [0,150)，镜 1 = [150,300)；序列总时长 300 帧。
    """
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<xmeml version="4">
  <sequence id="rve-sequence">
    <name>iv-fixture</name>
    <duration>{seq_duration}</duration>
    <rate><timebase>30</timebase><ntsc>FALSE</ntsc></rate>
    <timecode>
      <rate><timebase>30</timebase><ntsc>FALSE</ntsc></rate>
      <string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat>
    </timecode>
    <media>
      <video>
        <track>
          <clipitem id="clipitem-0">
            <name>shot_0</name><duration>150</duration>
            <start>0</start><end>150</end><in>0</in><out>150</out>
            <file id="file-1"><name>a.mp4</name></file>
          </clipitem>
          <clipitem id="clipitem-1">
            <name>shot_1</name><duration>150</duration>
            <start>{clip1_start}</start><end>{clip1_end}</end><in>150</in><out>300</out>
            <file id="file-1"/>
          </clipitem>
        </track>
      </video>
      <audio>
        <track>
          <clipitem id="clipitem-0">
            <name>shot_0</name><duration>150</duration>
            <start>0</start><end>150</end><in>0</in><out>150</out>
          </clipitem>
          <clipitem id="clipitem-1">
            <name>shot_1</name><duration>150</duration>
            <start>150</start><end>300</end><in>150</in><out>300</out>
          </clipitem>
        </track>
      </audio>
    </media>
  </sequence>
</xmeml>
"""


def _write_fixture(tmp_path: Path, xml_text: str) -> tuple[Path, Path]:
    xml = tmp_path / "fixture.fcp7xml.xml"
    xml.write_text(xml_text, encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_SPEC), encoding="utf-8")
    return xml, spec


# --------------------------------------------------------------------------- #
# 独立性：校验器不得依赖被验对象
# --------------------------------------------------------------------------- #
def test_verifier_does_not_import_rve() -> None:
    """硬要求：校验器一旦 import rve.* 就不再独立 —— 用源码断言钉死。"""
    text = VERIFIER_PATH.read_text(encoding="utf-8")
    offenders = [
        ln.strip()
        for ln in text.splitlines()
        if re.match(r"^\s*(import|from)\s+rve(\.|\b)", ln)
    ]
    assert not offenders, f"校验器出现了对 rve 的 import: {offenders}"


# --------------------------------------------------------------------------- #
# 真实产物：CLI 子进程现生成 → 校验器读回并与 spec.json 比对
# --------------------------------------------------------------------------- #
def test_real_product_passes_independent_verification(tmp_path: Path) -> None:
    iv = _load_verifier()
    ws = tmp_path / "ws"
    out = tmp_path / "out"

    proc = _run_cli(
        ["analyze", str(SYNTH), "--out", str(out)],
        _child_env(RVE_WORKSPACES=str(ws)),
    )
    assert proc.returncode == 0, f"CLI analyze 失败:\n{proc.stdout}\n{proc.stderr}"

    xmls = sorted(out.glob("*.fcp7xml.xml"))
    specs = sorted(ws.glob("*/spec.json"))
    assert xmls, f"未产出 FCP7 XML（检查 {out}）"
    assert specs, f"工作区未产出 spec.json（检查 {ws}）"

    ok, lines, result = iv.run(xmls[0], specs[0])
    assert ok, "独立校验未通过：\n" + "\n".join(lines)
    assert result["verdict"] == "PASS"


# --------------------------------------------------------------------------- #
# 故意坏掉的 XML：空洞 / 重叠 —— 校验器必须失败
# --------------------------------------------------------------------------- #
def test_valid_fixture_passes(tmp_path: Path) -> None:
    """先证明夹具本身是合法的（否则下面的失败可能是别的原因）。"""
    iv = _load_verifier()
    xml, spec = _write_fixture(tmp_path, _xml())
    ok, lines, _ = iv.run(xml, spec)
    assert ok, "合法夹具被误判为失败：\n" + "\n".join(lines)


def test_hole_is_rejected(tmp_path: Path) -> None:
    """洞：镜 1 起点 160 > 镜 0 终点 150（时间轴未铺满）。"""
    iv = _load_verifier()
    xml, spec = _write_fixture(tmp_path, _xml(clip1_start=160, clip1_end=310))
    ok, lines, _ = iv.run(xml, spec)
    assert not ok, "含空洞的 XML 被判为通过（漏检）：\n" + "\n".join(lines)
    assert any("空洞" in ln for ln in lines), "\n".join(lines)


def test_overlap_is_rejected(tmp_path: Path) -> None:
    """重叠：镜 1 起点 140 < 镜 0 终点 150。"""
    iv = _load_verifier()
    xml, spec = _write_fixture(tmp_path, _xml(clip1_start=140, clip1_end=290))
    ok, lines, _ = iv.run(xml, spec)
    assert not ok, "含重叠的 XML 被判为通过（漏检）：\n" + "\n".join(lines)
    assert any("重叠" in ln for ln in lines), "\n".join(lines)


def test_cli_exit_code_nonzero_on_broken_xml(tmp_path: Path) -> None:
    """命令行形态：坏 XML 必须非零退出（交付闸门可依赖退出码）。"""
    xml, spec = _write_fixture(tmp_path, _xml(clip1_start=160, clip1_end=310))
    proc = subprocess.run(
        [sys.executable, str(VERIFIER_PATH), str(xml), "--spec", str(spec)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode != 0, f"坏 XML 竟以 0 退出:\n{proc.stdout}"


# --------------------------------------------------------------------------- #
# 真实 NLE 产物形态（若本机存有对标样本则跑；否则跳过并注明）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sample",
    [
        "otiofcp_premiere_example.xml",
        "doublelove_xmeml_golden_25fps.xml",
        "empty_name_tags.xml",
        "hiero_xml_export.xml",
        "opentake_timeline-xmeml.xml",
        "sample_just_track.xml",
    ],
)
def test_real_nle_samples_are_parseable(tmp_path: Path, sample: str) -> None:
    """真实 NLE 导出的 xmeml 必须能被读取器吃下（结构模式不因真实形态误判）。"""
    real_dir = Path(os.environ.get("RVE_REAL_SAMPLES", str(Path(os.environ.get("TEMP", tmp_path)) / "rve_real_samples")))
    path = real_dir / sample
    if not path.exists():
        pytest.skip(f"对标样本不在本机（{path}）—— 样本不入库，见 findings.md")
    iv = _load_verifier()
    ok, lines, result = iv.run(path, None)
    assert ok, "读取器未能吃下真实样本：\n" + "\n".join(lines)
    assert result["verdict"] == "PASS"
