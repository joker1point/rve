"""交付验收（delivery gate）的 pytest 薄包装。

只覆盖**便宜且密闭**的阶段：
  - S0  预检（Python 版本 + 仓库布局）
  - S4  「现生成一次导出 → 对 FCP7 XML 做契约断言」
  - S7  开源卫生（LICENSE / README / .gitignore / 密钥 / vendor）
  - S8  CLI 口径一致性（doctor 输出的 pip 建议包名）

重的 venv / pip 阶段（S1 全量套件、S2 build+install、S3 analyze、S5 格式、S6 MCP）
留在 ``scripts/acceptance.py``，不进 pytest。

判据与脚本同源：断言的是**语义**（包名映射、时间轴不变量），不是文案字面量。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample_editspec.json"

HISTORICAL_BAD_PKG = "pyscenedetect"
CANON_PKG = {"scenedetect": "scenedetect", "opentimelineio": "opentimelineio", "mcp": "mcp"}
GITIGNORE_NEEDLES = [
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".rve/",
    "dist/",
    "build/",
    "*.egg-info/",
]
# 疑似密钥前缀（分段拼接：避免本扫描器把自己的模式定义当成命中）
SECRET_TOKENS = ["gh" + "o_", "gh" + "p_", "sk" + "-", "AK" + "IA"]
SKIP_DIRS = {
    ".git", ".pytest_cache", "dist", "build", "node_modules",
    ".venv", "venv", ".mypy_cache", ".ruff_cache",
}
TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".cfg", ".txt", ".json", ".yaml", ".yml", ".ini",
    ".in", ".sh", ".ps1", ".xml", ".html", ".css", ".js", ".ts", ".csv",
    ".gitignore", ".lock", ".rst", ".editorconfig", ".flake8",
}


# --------------------------------------------------------------------------- #
# 工具（与 scripts/acceptance.py 同源判据）
# --------------------------------------------------------------------------- #
def _pkg_name(spec: str) -> str:
    """从依赖声明取出规范化包名（去版本约束 / extras / markers）。"""
    return re.split(r"[<>=!~;\[\s(]", spec.strip())[0].strip().lower()


def _gitignore_covers(text: str, needle: str) -> bool:
    """语义判定 .gitignore 是否覆盖某类产物（``*.pyc`` 接受 ``*.py[cod]`` 等价写法）。"""
    if needle == "*.pyc":
        return bool(re.search(r"\*\.pyc\b", text) or re.search(r"\*\.py\[[a-z]+\]", text))
    return needle in text


def _valid_pkg_names() -> set[str]:
    """从 pyproject 声明的依赖（含 optional）取合法包名集合。"""
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    proj = data.get("project", {})
    names: set[str] = set()
    for dep in proj.get("dependencies", []) or []:
        names.add(_pkg_name(dep))
    for group in (proj.get("optional-dependencies") or {}).values():
        for dep in group or []:
            names.add(_pkg_name(dep))
    return names


def _check_advice_packages(text: str, valid: set[str], *, label: str) -> list[str]:
    """校验文本中每条 ``pip install '<pkg>'`` 建议的包名（语义判据）。"""
    problems: list[str] = []
    last_module: str | None = None
    for line in text.splitlines():
        m_mod = re.search(r"\(import\s+([A-Za-z_][\w.]*)\)", line)
        if m_mod:
            last_module = m_mod.group(1)
            continue
        for m_pkg in re.finditer(r"pip install\s+['\"]([^'\"]+)['\"]", line):
            name = _pkg_name(m_pkg.group(1))
            if name == HISTORICAL_BAD_PKG:
                problems.append(f"{label}: 历史错误包名 '{name}'")
            elif name not in valid:
                problems.append(f"{label}: 包名 '{name}' 不在项目依赖内 {sorted(valid)}")
            canon = CANON_PKG.get(last_module or "")
            if canon and name != canon:
                problems.append(f"{label}: {last_module} 建议 '{name}' 应为 '{canon}'")
    return problems


def _scan_secrets(root: Path) -> list[str]:
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if not (p.suffix.lower() in TEXT_SUFFIXES or p.suffix == ""):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for token in SECRET_TOKENS:
                if token in text:
                    hits.append(f"{p.relative_to(root)}: '{token}'")
    return hits


def _assert_fcp7_contract(xml_path: Path, expected_shots: int | None = None) -> list[str]:
    """FCP7 XML 契约断言（与交付闸门同源）。返回问题列表，空即通过。"""
    import xml.etree.ElementTree as ET

    problems: list[str] = []
    try:
        root = ET.parse(str(xml_path)).getroot()
        if root.tag != "xmeml":
            problems.append(f"根标签应为 'xmeml'，实为 '{root.tag}'")
        if not root.get("version"):
            problems.append("<xmeml> 缺 version 属性")

        seq = root.find("sequence")
        if seq is None:
            return problems + ["缺 <sequence>"]
        if seq.find("rate/timebase") is None:
            problems.append("sequence 缺 <rate><timebase>")
        dur_el = seq.find("duration")
        duration = int(dur_el.text) if dur_el is not None and dur_el.text else None
        if duration is None:
            problems.append("sequence 缺/坏 <duration>")

        track = seq.find("media/video/track")
        if track is None:
            return problems + ["缺 <media><video><track>"]
        children = list(track)
        clipitems = [c for c in children if c.tag == "clipitem"]
        if not clipitems:
            return problems + ["video track 内无 <clipitem>"]

        spans = [(int(c.find("start").text), int(c.find("end").text)) for c in clipitems]
        if spans[0][0] != 0:
            problems.append(f"首个 clipitem start 应为 0，实为 {spans[0][0]}")
        for i, (prev, cur) in enumerate(zip(spans, spans[1:])):
            if cur[0] != prev[1]:
                problems.append(f"clipitem {i}->{i + 1} 不连续/重叠: {prev[1]} != {cur[0]}")
        if duration is not None and spans[-1][1] != duration:
            problems.append(f"末 clipitem end {spans[-1][1]} != sequence duration {duration}")

        for i, c in enumerate(clipitems):
            in_f, out_f, dur_f = (
                int(c.find("in").text), int(c.find("out").text), int(c.find("duration").text)
            )
            if out_f - in_f != dur_f:
                problems.append(f"clipitem {i}: out-in {out_f - in_f} != duration {dur_f}")
            if spans[i][1] - spans[i][0] != dur_f:
                problems.append(f"clipitem {i}: end-start != duration {dur_f}")

        for idx, c in enumerate(children):
            if c.tag != "transitionitem":
                continue
            cut = next(
                (int(nxt.find("start").text) for nxt in children[idx + 1:] if nxt.tag == "clipitem"),
                None,
            )
            s, e = int(c.find("start").text), int(c.find("end").text)
            if cut is None:
                problems.append(f"transitionitem@{idx} 之后无 clipitem，无法定剪切点")
            elif not (s < cut < e):
                problems.append(f"transitionitem@{idx} 未跨骑剪切点: [{s}, {e}) cut={cut}")

        if expected_shots is not None and len(clipitems) != expected_shots:
            problems.append(f"clipitem 数 {len(clipitems)} != EditSpec 镜头数 {expected_shots}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"解析 XML 失败: {type(exc).__name__}: {exc}")
    return problems


def _load_fixture_spec():
    from rve.schema.editspec import EditSpec

    return EditSpec.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# S0 预检
# --------------------------------------------------------------------------- #
def test_s0_repo_layout_and_python_version() -> None:
    assert sys.version_info >= (3, 11), f"需要 Python >= 3.11，实为 {sys.version}"
    for rel in ("pyproject.toml", "src/rve", "tests", "tests/data/synth_cut.mp4"):
        assert (REPO_ROOT / rel).exists(), f"仓库布局缺失: {rel}"


# --------------------------------------------------------------------------- #
# S4 FCP7 XML 契约（现生成一次导出再断言）
# --------------------------------------------------------------------------- #
def test_s4_fcp7_export_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from rve.pipeline.export import export_timeline
    from rve.schema.enums import ExportFormat

    spec = _load_fixture_spec()
    ws = tmp_path / "ws"
    (ws / spec.spec_id).mkdir(parents=True)
    (ws / spec.spec_id / "spec.json").write_text(spec.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("RVE_WORKSPACES", str(ws))

    out = tmp_path / f"{spec.spec_id}.fcp7xml.xml"
    result = asyncio.run(export_timeline(spec.spec_id, ExportFormat.FCP7XML, str(out)))
    assert result.receipt.ok

    problems = _assert_fcp7_contract(Path(result.out_path), expected_shots=len(spec.shots))
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------- #
# S7 开源卫生
# --------------------------------------------------------------------------- #
def test_s7_open_source_hygiene() -> None:
    assert (REPO_ROOT / "LICENSE").exists(), "缺 LICENSE"
    assert (REPO_ROOT / "README.md").exists(), "缺 README.md"

    gitignore = REPO_ROOT / ".gitignore"
    assert gitignore.exists(), "缺 .gitignore"
    text = gitignore.read_text(encoding="utf-8", errors="ignore")
    missed = [n for n in GITIGNORE_NEEDLES if not _gitignore_covers(text, n)]
    assert not missed, f".gitignore 未覆盖: {missed}"

    hits = _scan_secrets(REPO_ROOT)
    assert not hits, f"仓库内发现疑似密钥: {hits}"

    for d in ("vendor", "third_party"):
        p = REPO_ROOT / d
        assert not (p.is_dir() and any(p.rglob("*"))), f"存在 vendored 上游源码树: {d}/"


# --------------------------------------------------------------------------- #
# S8 CLI 口径一致性
# --------------------------------------------------------------------------- #
def test_s8_doctor_package_names_are_canonical(capsys: pytest.CaptureFixture) -> None:
    from rve.cli import main

    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0, f"`rve doctor` 退出码 {code}"

    valid = _valid_pkg_names()
    problems = _check_advice_packages(out, valid, label="doctor 输出")
    assert HISTORICAL_BAD_PKG not in out, f"doctor 输出出现 '{HISTORICAL_BAD_PKG}'"

    cli_src = REPO_ROOT / "src" / "rve" / "cli.py"
    if cli_src.exists():
        src_text = cli_src.read_text(encoding="utf-8", errors="ignore")
        problems += _check_advice_packages(src_text, valid, label="cli.py 源码")
        assert HISTORICAL_BAD_PKG not in src_text, f"cli.py 出现 '{HISTORICAL_BAD_PKG}'"

    assert not problems, "\n".join(problems)
