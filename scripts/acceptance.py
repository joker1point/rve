#!/usr/bin/env python
"""RVE 交付验收（delivery gate）—— 一条命令验完真实交付形态。

用法（仓库根目录运行）::

    python scripts/acceptance.py

逐阶段打印 PASS/FAIL/SKIP 报告 + 末尾汇总；只有**全过**（无 FAIL）才 exit 0。

设计约束
--------
- 编排只依赖标准库；只有子进程里才出现 pip / pytest / build。
- 密闭：全部产物落在系统临时目录，通过 ``RVE_WORKSPACES`` 把工作区也指到临时目录，
  跑完清理；除本文件（及配套测试）外不往仓库写任何东西。
- 某阶段本机确实跑不了（如无网络）→ 记 FAIL 并写明原因，绝不静默跳过。
  唯一允许的 SKIP 是 ``opentimelineio`` 缺席时的 OTIO 往返子检查（已在阶段内显式注明）。
- 末行输出机器可读结果：``RESULT: PASS`` / ``RESULT: FAIL``。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTH = REPO_ROOT / "tests" / "data" / "synth_cut.mp4"

# 历史上把 scenedetect 误写成 pyscenedetect —— 交付闸门必须挡住它
HISTORICAL_BAD_PKG = "pyscenedetect"
# 模块名 → 正确的 PyPI 包名（用于"建议口径"语义比对）
CANON_PKG = {
    "scenedetect": "scenedetect",
    "opentimelineio": "opentimelineio",
    "mcp": "mcp",
}
# .gitignore 必须覆盖的条目
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
# 扫描时跳过的目录 / 二进制后缀
SKIP_DIRS = {".git", ".pytest_cache", "dist", "build", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache"}
TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".cfg", ".txt", ".json", ".yaml", ".yml", ".ini",
    ".in", ".sh", ".ps1", ".xml", ".html", ".css", ".js", ".ts", ".csv",
    ".gitignore", ".lock", ".rst", ".editorconfig", ".flake8",
}

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

StageFn = Callable[["Context"], "tuple[str, list[str]]"]


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
class Context:
    """跨阶段传递的密闭状态（临时目录 / venv / 产物路径 / 输出）。"""

    def __init__(self, tmp_root: Path) -> None:
        self.tmp = tmp_root
        self.venv_dir: Path | None = None
        self.venv_py: Path | None = None
        self.venv_rve: Path | None = None
        self.install_method: str = ""
        self.doctor_out: str = ""
        self.ws: Path | None = None
        self.spec_id: str | None = None
        self.xml_path: Path | None = None
        self.spec_path: Path | None = None


def _run(
    cmd: list[object],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 900,
) -> tuple[int, str, str]:
    """执行子进程，返回 (rc, stdout, stderr)；文本按 UTF-8 容错解码。"""
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _venv_env(ws: Path | None = None) -> dict[str, str]:
    """构造子进程环境：清掉 PYTHONPATH（确保命中 venv 内已安装的 wheel），可选设置工作区。"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    if ws is not None:
        env["RVE_WORKSPACES"] = str(ws)
    else:
        env.pop("RVE_WORKSPACES", None)
    return env


def _venv_python(venv_dir: Path) -> Path | None:
    for rel in ("Scripts/python.exe", "bin/python", "Scripts/python"):
        p = venv_dir / rel
        if p.exists():
            return p
    return None


def _venv_rve(venv_dir: Path) -> Path | None:
    for rel in ("Scripts/rve.exe", "bin/rve"):
        p = venv_dir / rel
        if p.exists():
            return p
    return None


def _pkg_name(spec: str) -> str:
    """从依赖声明里取出规范化包名（去版本约束、extras、markers）。"""
    return re.split(r"[<>=!~;\[\s(]", spec.strip())[0].strip().lower()


def _gitignore_covers(text: str, needle: str) -> bool:
    """语义判定 .gitignore 是否覆盖某类产物（不苛求字面写法）。

    ``*.pyc`` 接受 ``*.py[cod]`` / ``*.py[co]`` 等价写法——只要字节码被覆盖即可。
    """
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


def check_advice_packages(text: str, valid: set[str], *, label: str) -> tuple[list[str], bool]:
    """校验文本中每条 ``pip install '<pkg>'`` 建议的包名。

    判据是**语义**而非文案：包名必须落在项目声明的依赖集合内，且若其前一行给出了
    ``(import <module>)``，包名必须等于该模块对应的规范 PyPI 名。

    返回 (问题列表, 是否出现过建议行)。历史错误包名 ``pyscenedetect`` 无论如何都会被挡下。
    """
    problems: list[str] = []
    found = False
    last_module: str | None = None
    for line in text.splitlines():
        m_mod = re.search(r"\(import\s+([A-Za-z_][\w.]*)\)", line)
        if m_mod:
            last_module = m_mod.group(1)
            continue
        for m_pkg in re.finditer(r"pip install\s+['\"]([^'\"]+)['\"]", line):
            found = True
            name = _pkg_name(m_pkg.group(1))
            if name == HISTORICAL_BAD_PKG:
                problems.append(f"{label}: 建议引用历史错误包名 '{name}'（应为 scenedetect）")
            elif name not in valid:
                problems.append(
                    f"{label}: 建议包名 '{name}' 不在项目声明的依赖内 {sorted(valid)}"
                )
            canon = CANON_PKG.get(last_module or "")
            if canon and name != canon:
                problems.append(
                    f"{label}: 模块 {last_module} 的建议 '{name}' 与规范包名 '{canon}' 不符"
                )
    return problems, found


def _iter_text_files(root: Path):
    """遍历 root 下的文本文件（跳过忽略目录与二进制后缀）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in TEXT_SUFFIXES or p.suffix == "":
                yield p


def scan_secrets(root: Path) -> list[str]:
    """扫描疑似密钥前缀，返回命中列表。"""
    hits: list[str] = []
    for p in _iter_text_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for token in SECRET_TOKENS:
            if token in text:
                hits.append(f"{p.relative_to(root)}: 命中疑似密钥前缀 '{token}'")
    return hits


def assert_fcp7_contract(
    xml_path: Path, expected_shots: int | None = None
) -> list[str]:
    """FCP7 XML 交付契约断言（核心验收）。

    不变量：
      1. ``<xmeml>`` 有 ``version``；
      2. sequence ``duration`` == 最后一个 clipitem 的 ``<end>``；
      3. video track 的 clipitems 在序列时间轴上从 0 起、累积且连续、无缝隙无重叠地铺满；
      4. 每个 clipitem 的 ``<in>/<out>`` 与 ``<duration>`` 自洽；
      5. ``<rate><timebase>`` 存在；
      6. 若有 ``<transitionitem>``，必须跨骑其剪切点（start < cut < end）。
    """
    problems: list[str] = []
    try:
        root = ET.parse(str(xml_path)).getroot()
        if root.tag != "xmeml":
            problems.append(f"根标签应为 'xmeml'，实为 '{root.tag}'")
        if not root.get("version"):
            problems.append("<xmeml> 缺 version 属性")

        seq = root.find("sequence")
        if seq is None:
            problems.append("缺 <sequence>")
            return problems

        if seq.find("rate/timebase") is None:
            problems.append("sequence 缺 <rate><timebase>")

        dur_el = seq.find("duration")
        duration = int(dur_el.text) if dur_el is not None and dur_el.text else None
        if duration is None:
            problems.append("sequence 缺/坏 <duration>")

        track = seq.find("media/video/track")
        if track is None:
            problems.append("缺 <media><video><track>")
            return problems

        children = list(track)
        clipitems = [c for c in children if c.tag == "clipitem"]
        if not clipitems:
            problems.append("video track 内无 <clipitem>")
            return problems

        spans = [(int(c.find("start").text), int(c.find("end").text)) for c in clipitems]
        if spans[0][0] != 0:
            problems.append(f"首个 clipitem 的 start 应为 0，实为 {spans[0][0]}")
        for i, (prev, cur) in enumerate(zip(spans, spans[1:])):
            if cur[0] != prev[1]:
                problems.append(
                    f"clipitem {i}->{i + 1} 在时间轴上不连续/重叠: {prev[1]} != {cur[0]}"
                )
        if duration is not None and spans[-1][1] != duration:
            problems.append(
                f"末个 clipitem end {spans[-1][1]} != sequence duration {duration}"
            )

        for i, c in enumerate(clipitems):
            in_f = int(c.find("in").text)
            out_f = int(c.find("out").text)
            dur_f = int(c.find("duration").text)
            if out_f - in_f != dur_f:
                problems.append(
                    f"clipitem {i}: out-in={out_f - in_f} 与 <duration>={dur_f} 不自洽"
                )
            if spans[i][1] - spans[i][0] != dur_f:
                problems.append(
                    f"clipitem {i}: end-start={spans[i][1] - spans[i][0]} 与 <duration>={dur_f} 不自洽"
                )

        for idx, c in enumerate(children):
            if c.tag != "transitionitem":
                continue
            cut = None
            for nxt in children[idx + 1:]:
                if nxt.tag == "clipitem":
                    cut = int(nxt.find("start").text)
                    break
            s = int(c.find("start").text)
            e = int(c.find("end").text)
            if cut is None:
                problems.append(f"transitionitem@{idx} 之后无 clipitem，无法确定剪切点")
            elif not (s < cut < e):
                problems.append(
                    f"transitionitem@{idx} 未跨骑剪切点: [{s}, {e}) 未包住 cut={cut}"
                )

        if expected_shots is not None and len(clipitems) != expected_shots:
            problems.append(
                f"clipitem 数 {len(clipitems)} != EditSpec 镜头数 {expected_shots}"
            )
    except Exception as exc:  # noqa: BLE001 —— 解析期任何异常都算契约不成立
        problems.append(f"解析 XML 失败: {type(exc).__name__}: {exc}")
    return problems


# --------------------------------------------------------------------------- #
# 阶段实现
# --------------------------------------------------------------------------- #
def stage_s0_preflight(ctx: Context) -> tuple[str, list[str]]:
    """S0 预检：Python 版本 + 仓库布局。"""
    detail: list[str] = []
    problems: list[str] = []

    ver = ".".join(str(n) for n in sys.version_info[:3])
    if sys.version_info < (3, 11):
        problems.append(f"Python 版本 {ver} < 3.11")
    else:
        detail.append(f"Python {ver} >= 3.11")
    detail.append(f"解释器: {sys.executable}")

    for rel in ("pyproject.toml", "src/rve", "tests", "tests/data/synth_cut.mp4"):
        if (REPO_ROOT / rel).exists():
            detail.append(f"布局 OK: {rel}")
        else:
            problems.append(f"缺失: {rel}")

    return (FAIL if problems else PASS), detail + problems


def stage_s1_tests(ctx: Context) -> tuple[str, list[str]]:
    """S1 单测套件：子进程跑 pytest，要求 exit 0 且 0 failed / 0 errors。"""
    rc, out, err = _run(
        [sys.executable, "-m", "pytest", "-q"], cwd=REPO_ROOT, timeout=1200
    )
    tail = "\n".join([ln for ln in (out.strip().splitlines() + err.strip().splitlines()) if ln][-6:])

    def _count(word: str) -> int:
        m = re.search(rf"(\d+)\s+{word}s?\b", out)
        return int(m.group(1)) if m else 0

    failed = _count("failed")
    errors = _count("error")
    passed = _count("passed")
    skipped = _count("skipped")

    detail = [
        f"pytest 退出码: {rc}",
        f"passed={passed} skipped={skipped} failed={failed} errors={errors}",
        f"摘要末行: {tail.splitlines()[-1] if tail else '(无)'}",
    ]
    if rc != 0 or failed or errors:
        if rc != 0 and not (failed or errors):
            detail.append("退出码非 0 且未见 failed/errors —— 可能是收集/导入错误")
        return FAIL, detail
    return PASS, detail


def _ensure_build_python(ctx: Context) -> tuple[Path | None, list[str]]:
    """返回可执行 ``python -m build`` 的解释器；本机无 build 时只装进临时 venv。"""
    import importlib.util

    detail: list[str] = []
    if importlib.util.find_spec("build") is not None:
        detail.append("宿主已具备 build 包，直接用宿主解释器构建")
        return Path(sys.executable), detail

    bv = ctx.tmp / "buildvenv"
    rc, _, err = _run([sys.executable, "-m", "venv", str(bv)])
    if rc != 0:
        detail.append(f"创建构建 venv 失败: {err.strip()[-400:]}")
        return None, detail
    bpy = _venv_python(bv)
    if bpy is None:
        detail.append("构建 venv 中找不到 python")
        return None, detail
    rc, _, err = _run([bpy, "-m", "pip", "install", "build"], timeout=600)
    if rc != 0:
        detail.append(f"在临时 venv 安装 build 失败: {err.strip()[-400:]}")
        return None, detail
    detail.append("宿主无 build：已仅装入临时构建 venv（绝不写全局环境）")
    return bpy, detail


def stage_s2_build_install(ctx: Context) -> tuple[str, list[str]]:
    """S2 构建 wheel + 全新 venv 安装（这才是真交付形态）+ rve --help / doctor。"""
    detail: list[str] = []
    s2 = ctx.tmp / "s2"
    wheelhouse = s2 / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)

    build_py, bdetail = _ensure_build_python(ctx)
    detail += bdetail
    if build_py is None:
        return FAIL, detail + ["无法构建 wheel（无 build 且无法装入临时 venv）"]

    rc, out, err = _run(
        [build_py, "-m", "build", "--wheel", "--outdir", str(wheelhouse), str(REPO_ROOT)],
        cwd=REPO_ROOT,
        timeout=900,
    )
    if rc != 0:
        return FAIL, detail + [
            f"`python -m build --wheel` 失败 (rc={rc})",
            (out + err).strip()[-600:],
        ]
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        return FAIL, detail + ["构建成功但未产出 .whl 文件"]
    wheel = wheels[0]
    detail.append(f"wheel: {wheel.name}")

    venv_dir = s2 / "venv"
    rc, _, err = _run([sys.executable, "-m", "venv", str(venv_dir)], timeout=300)
    if rc != 0:
        return FAIL, detail + [f"创建全新 venv 失败: {err.strip()[-400:]}"]
    vpy = _venv_python(venv_dir)
    if vpy is None:
        return FAIL, detail + ["全新 venv 中找不到 python 可执行文件"]

    # 主路径：带依赖安装（真交付形态）
    rc, out, err = _run([vpy, "-m", "pip", "install", str(wheel)], timeout=900)
    method = "全新 venv + pip install <wheel>（带依赖，真交付形态）"
    if rc != 0:
        # 无网络兜底：system-site-packages + --no-deps
        detail.append("主路径 pip 安装失败，切换到无网络兜底路径")
        detail.append((out + err).strip()[-400:])
        venv_dir = s2 / "venv-sys"
        rc2, _, err2 = _run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
            timeout=300,
        )
        if rc2 != 0:
            return FAIL, detail + [f"兜底 venv 创建失败: {err2.strip()[-400:]}"]
        vpy = _venv_python(venv_dir)
        if vpy is None:
            return FAIL, detail + ["兜底 venv 中找不到 python"]
        rc, out, err = _run(
            [vpy, "-m", "pip", "install", "--no-deps", str(wheel)], timeout=900
        )
        method = "兜底：venv --system-site-packages + pip install --no-deps（包独立验证，依赖来自宿主）"
        if rc != 0:
            return FAIL, detail + [
                "兜底路径 pip 安装也失败（疑无网络且宿主缺依赖）",
                (out + err).strip()[-600:],
            ]
    ctx.venv_dir = venv_dir
    ctx.venv_py = vpy
    ctx.venv_rve = _venv_rve(venv_dir)
    ctx.install_method = method
    detail.append(f"安装路径: {method}")

    rve_cmd = [ctx.venv_rve] if ctx.venv_rve else [vpy, "-m", "rve.cli"]
    if ctx.venv_rve is None:
        detail.append("venv 内无 rve 控制台脚本，回退 python -m rve.cli")

    rc, out, err = _run(rve_cmd + ["--help"], env=_venv_env(), timeout=120)
    if rc != 0:
        return FAIL, detail + [f"`rve --help` 退出码 {rc}", (out + err).strip()[-400:]]
    detail.append("`rve --help` exit 0")

    doctor_ws = s2 / "ws-doctor"
    rc, out, err = _run(rve_cmd + ["doctor"], env=_venv_env(doctor_ws), timeout=300)
    ctx.doctor_out = out
    if rc != 0:
        return FAIL, detail + [f"`rve doctor` 退出码 {rc}", (out + err).strip()[-400:]]
    detail.append("`rve doctor` exit 0")

    valid = _valid_pkg_names()
    probs, found = check_advice_packages(out, valid, label="doctor 输出")
    if HISTORICAL_BAD_PKG in out:
        probs.append(f"doctor 输出出现不存在的 PyPI 包名 '{HISTORICAL_BAD_PKG}'")
    detail.append(
        "doctor 输出的 pip 建议行: "
        + ("已全部校验通过" if found else "无（所有依赖已就绪，未打印建议）")
    )
    if probs:
        return FAIL, detail + probs
    return PASS, detail


def stage_s3_analyze(ctx: Context) -> tuple[str, list[str]]:
    """S3 全新 venv 内端到端 analyze（密闭：工作区/产物都在临时目录）。"""
    if ctx.venv_py is None:
        return FAIL, ["S2 未建立 venv，无法在真交付形态下跑 analyze"]
    detail: list[str] = []
    s3 = ctx.tmp / "s3"
    ws = s3 / "ws"
    out_dir = s3 / "out"
    ws.mkdir(parents=True, exist_ok=True)

    rve_cmd = [ctx.venv_rve] if ctx.venv_rve else [ctx.venv_py, "-m", "rve.cli"]
    rc, out, err = _run(
        rve_cmd + ["analyze", str(SYNTH), "--out", str(out_dir)],
        cwd=REPO_ROOT,
        env=_venv_env(ws),
        timeout=900,
    )
    if rc != 0:
        return FAIL, detail + [f"`rve analyze` 退出码 {rc}", (out + err).strip()[-600:]]
    detail.append(f"analyze exit 0; stdout: {out.strip()[-200:]}")

    xmls = sorted(out_dir.glob("*.fcp7xml.xml"))
    if not xmls:
        return FAIL, detail + [f"未产出 *.fcp7xml.xml（检查 {out_dir}）"]
    specs = sorted(ws.glob("*/spec.json"))
    if not specs:
        return FAIL, detail + [f"工作区未产出 spec.json（检查 {ws}）"]

    ctx.ws = ws
    ctx.xml_path = xmls[0]
    ctx.spec_path = specs[0]
    ctx.spec_id = specs[0].parent.name
    detail.append(f"FCP7 XML: {xmls[0]}")
    detail.append(f"工作区 spec.json: {specs[0]} (spec_id={ctx.spec_id})")
    return PASS, detail


def stage_s4_xml_contract(ctx: Context) -> tuple[str, list[str]]:
    """S4 FCP7 XML 契约断言（核心验收）。"""
    if ctx.xml_path is None or not ctx.xml_path.exists():
        return FAIL, ["S3 未产出可供断言的 FCP7 XML"]
    expected = None
    if ctx.spec_path is not None and ctx.spec_path.exists():
        expected = len(json.loads(ctx.spec_path.read_text(encoding="utf-8")).get("shots", []))
    problems = assert_fcp7_contract(ctx.xml_path, expected_shots=expected)
    detail = [
        f"断言对象: {ctx.xml_path.name}",
        f"EditSpec 镜头数(期望 clipitem 数): {expected}",
    ]
    if problems:
        return FAIL, detail + problems
    detail.append("全部契约不变量成立：version / duration==末end / 连续铺满 / in-out自洽 / timebase / 转场跨骑")
    return PASS, detail


def stage_s5_formats(ctx: Context) -> tuple[str, list[str]]:
    """S5 格式面：EDL 与 OTIO 各产出非空文件；OTIO 往返（缺 otio 时 skip 子检查）。"""
    if ctx.spec_id is None or ctx.ws is None:
        return FAIL, ["S3 未产出 spec_id/工作区，无法验证导出格式"]
    detail: list[str] = []
    problems: list[str] = []
    s5 = ctx.tmp / "s5"
    s5.mkdir(parents=True, exist_ok=True)
    rve_cmd = [ctx.venv_rve] if ctx.venv_rve else [ctx.venv_py, "-m", "rve.cli"]

    outputs: dict[str, Path] = {
        "edl": s5 / f"{ctx.spec_id}.edl",
        "otio": s5 / f"{ctx.spec_id}.otio",
    }
    for fmt, out_path in outputs.items():
        rc, out, err = _run(
            rve_cmd + ["export", ctx.spec_id, "--format", fmt, "--out", str(out_path)],
            cwd=REPO_ROOT,
            env=_venv_env(ctx.ws),
            timeout=300,
        )
        if rc != 0:
            problems.append(
                f"`rve export --format {fmt}` 退出码 {rc}: {(out + err).strip()[-400:]}"
            )
        elif not out_path.exists() or out_path.stat().st_size == 0:
            problems.append(f"`rve export --format {fmt}` 未产出非空文件 ({out_path})")
        else:
            detail.append(f"{fmt}: {out_path.name} ({out_path.stat().st_size} bytes)")

    # OTIO 往返：读回并断言镜头数 == EditSpec 镜头数
    otio_path = outputs["otio"]
    probe_rc, _, _ = _run(
        [ctx.venv_py, "-c", "import opentimelineio"], env=_venv_env(), timeout=120
    )
    if probe_rc != 0:
        detail.append("OTIO 往返: SKIP —— venv 内无 opentimelineio（已显式注明，非静默跳过）")
    elif otio_path.exists() and otio_path.stat().st_size > 0:
        code = (
            "import opentimelineio as otio, sys;"
            "tl = otio.adapters.read_from_file(sys.argv[1]);"
            "print(sum(1 for it in tl.tracks[0] if isinstance(it, otio.schema.Clip)))"
        )
        rc, out, err = _run(
            [ctx.venv_py, "-c", code, str(otio_path)], env=_venv_env(), timeout=300
        )
        if rc != 0:
            problems.append(f"OTIO 读回失败: {(out + err).strip()[-400:]}")
        else:
            try:
                count = int(out.strip().splitlines()[-1])
            except (ValueError, IndexError):
                problems.append(f"OTIO 读回镜头数解析失败: {out.strip()[-200:]}")
                count = None
            if count is not None:
                expected = len(
                    json.loads(ctx.spec_path.read_text(encoding="utf-8")).get("shots", [])
                )
                if count != expected:
                    problems.append(f"OTIO 镜头数 {count} != EditSpec 镜头数 {expected}")
                else:
                    detail.append(f"OTIO 往返 OK: 视频轨 Clip 数 {count} == EditSpec 镜头数")
    else:
        detail.append("OTIO 往返: 跳过（OTIO 文件未产出）")

    if problems:
        return FAIL, detail + problems
    return PASS, detail


# 探针需同时兼容 mcp 1.x / 2.x：Tool 的 schema 字段在 1.x 叫 inputSchema、
# 2.x 改名为 input_schema；ResourceTemplate 的 uriTemplate 同理（1.x camel / 2.x snake）。
# 容器要以"真交付形态"（全新 venv 解析到 2.x）与宿主（1.x）两种环境都跑得动，
# 故此处按可用性取值，而不是写死任一版属性名。
_MCP_PROBE = r'''
import asyncio, json
from rve.mcp.server import build_server

def _pick(obj, *names):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None

srv = build_server()
tools = asyncio.run(srv.list_tools())
templates = asyncio.run(srv.list_resource_templates())
print(json.dumps({
    "tools": {t.name: _pick(t, "inputSchema", "input_schema") for t in tools},
    "templates": [_pick(t, "uriTemplate", "uri_template") for t in templates],
}))
'''


def stage_s6_mcp(ctx: Context) -> tuple[str, list[str]]:
    """S6 MCP 面：全新 venv 内枚举注册工具，断言工具名与 input schema 合法。"""
    if ctx.venv_py is None:
        return FAIL, ["S2 未建立 venv，无法验证 MCP 面"]
    expected_tools = {
        "probe_video",
        "detect_shots",
        "analyze_audio",
        "describe_shots",
        "build_edit_spec",
        "get_edit_spec",
        "export_timeline",
        "render_report",
    }
    probe = ctx.tmp / "s6" / "mcp_probe.py"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(_MCP_PROBE, encoding="utf-8")

    rc, out, err = _run([ctx.venv_py, str(probe)], env=_venv_env(), timeout=300)
    if rc != 0:
        return FAIL, [f"MCP 枚举探针退出码 {rc}", (out + err).strip()[-600:]]

    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    try:
        data = json.loads(lines[-1])
    except (ValueError, IndexError) as exc:
        return FAIL, [f"无法解析 MCP 探针输出: {exc}", out.strip()[-400:]]

    tool_schemas: dict = data.get("tools", {})
    templates: list = data.get("templates", [])
    detail = [f"已注册工具数: {len(tool_schemas)}", f"资源模板: {templates}"]

    problems: list[str] = []
    missing = expected_tools - set(tool_schemas)
    if missing:
        problems.append(f"缺失期望工具: {sorted(missing)}")

    for name in sorted(expected_tools & set(tool_schemas)):
        schema = tool_schemas[name]
        if not isinstance(schema, dict):
            problems.append(f"工具 {name} 的 input schema 非 dict")
            continue
        if schema.get("type") != "object":
            problems.append(f"工具 {name} 的 input schema type != 'object'（{schema.get('type')!r}）")
        if not isinstance(schema.get("properties"), dict):
            problems.append(f"工具 {name} 的 input schema 缺 properties")
        if "required" not in schema:
            problems.append(f"工具 {name} 的 input schema 缺 required 字段")
    detail.append("每个期望工具都有合法 input schema" if not problems else "schema 校验存在问题")

    if "editspec://{spec_id}" not in templates:
        problems.append("缺 editspec://{spec_id} 资源模板")
    else:
        detail.append("editspec 资源模板存在")

    if problems:
        return FAIL, detail + problems
    return PASS, detail


def stage_s7_hygiene(ctx: Context) -> tuple[str, list[str]]:
    """S7 开源卫生：LICENSE/README/.gitignore/密钥/vendor。"""
    detail: list[str] = []
    problems: list[str] = []

    if (REPO_ROOT / "LICENSE").exists():
        detail.append("LICENSE 存在")
    else:
        problems.append("缺 LICENSE")

    if (REPO_ROOT / "README.md").exists():
        detail.append("README.md 存在")
    else:
        problems.append("缺 README.md")

    gi = REPO_ROOT / ".gitignore"
    if not gi.exists():
        problems.append("缺 .gitignore")
    else:
        text = gi.read_text(encoding="utf-8", errors="ignore")
        missed = [n for n in GITIGNORE_NEEDLES if not _gitignore_covers(text, n)]
        if missed:
            problems.append(f".gitignore 未覆盖: {missed}")
        else:
            detail.append(".gitignore 覆盖全部必需条目")

    hits = scan_secrets(REPO_ROOT)
    if hits:
        problems += [f"疑似密钥: {h}" for h in hits[:10]]
    else:
        detail.append("未发现疑似密钥前缀")

    for d in ("vendor", "third_party"):
        p = REPO_ROOT / d
        if p.is_dir() and any(p.rglob("*")):
            problems.append(f"存在 vendored 上游源码树: {d}/")

    if problems:
        return FAIL, detail + problems
    return PASS, detail


def stage_s8_cli_consistency(ctx: Context) -> tuple[str, list[str]]:
    """S8 CLI 口径一致性：doctor 输出 + doctor 源码的 pip 建议包名必须正确。"""
    detail: list[str] = []
    problems: list[str] = []
    valid = _valid_pkg_names()
    detail.append(f"项目声明的合法包名: {sorted(valid)}")

    if ctx.doctor_out:
        probs, found = check_advice_packages(ctx.doctor_out, valid, label="doctor 输出")
        if HISTORICAL_BAD_PKG in ctx.doctor_out:
            probs.append(f"doctor 输出出现 '{HISTORICAL_BAD_PKG}'")
        detail.append(
            "doctor 输出建议行: " + ("已校验通过" if found else "无（依赖齐备，未打印建议）")
        )
        problems += probs
    else:
        detail.append("doctor 输出为空（S2 可能未跑到 doctor）")

    # 补充闸门：即使依赖齐备、doctor 不打印建议，也要挡住源码里写死的错包名
    cli_src = REPO_ROOT / "src" / "rve" / "cli.py"
    if cli_src.exists():
        probs2, found2 = check_advice_packages(
            cli_src.read_text(encoding="utf-8", errors="ignore"), valid, label="cli.py 源码"
        )
        if HISTORICAL_BAD_PKG in cli_src.read_text(encoding="utf-8", errors="ignore"):
            probs2.append(f"cli.py 源码出现 '{HISTORICAL_BAD_PKG}'")
        detail.append(
            "cli.py 建议串: " + ("已校验通过" if found2 else "无")
        )
        problems += probs2

    if problems:
        return FAIL, detail + problems
    return PASS, detail


STAGES: list[tuple[str, str, StageFn]] = [
    ("S0", "预检（Python>=3.11 + 仓库布局）", stage_s0_preflight),
    ("S1", "单测套件（pytest -q，exit 0 且 0 failed/errors）", stage_s1_tests),
    ("S2", "构建 wheel + 全新 venv 安装 + rve --help/doctor", stage_s2_build_install),
    ("S3", "全新 venv 内端到端 analyze（密闭）", stage_s3_analyze),
    ("S4", "FCP7 XML 契约断言（核心验收）", stage_s4_xml_contract),
    ("S5", "格式面（EDL / OTIO + OTIO 往返）", stage_s5_formats),
    ("S6", "MCP 面（工具名 + input schema）", stage_s6_mcp),
    ("S7", "开源卫生（LICENSE/README/.gitignore/密钥/vendor）", stage_s7_hygiene),
    ("S8", "CLI 口径一致性（包名闸门）", stage_s8_cli_consistency),
]


def main() -> int:
    print("=" * 70)
    print("RVE 交付验收（delivery gate）")
    print(f"仓库: {REPO_ROOT}")
    print(f"解释器: {sys.executable}")
    print("=" * 70)

    tmp_root = Path(tempfile.mkdtemp(prefix="rve-accept-"))
    ctx = Context(tmp_root)
    results: list[tuple[str, str, str, list[str]]] = []
    try:
        for sid, title, fn in STAGES:
            print(f"\n[{sid}] {title}")
            try:
                status, detail = fn(ctx)
            except Exception as exc:  # noqa: BLE001 —— 阶段异常一律记 FAIL，不静默中断
                status = FAIL
                detail = [f"阶段异常: {type(exc).__name__}: {exc}"]
            results.append((sid, title, status, detail))
            for line in detail:
                print(f"    - {line}")
            print(f"    => {sid}: {status}")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print("\n" + "=" * 70)
    print("汇总")
    print("-" * 70)
    for sid, title, status, _ in results:
        print(f"  {sid:>2}  {status:<4}  {title}")
    print("-" * 70)
    n_fail = sum(1 for r in results if r[2] == FAIL)
    n_pass = sum(1 for r in results if r[2] == PASS)
    n_skip = sum(1 for r in results if r[2] == SKIP)
    print(f"通过 {n_pass} / 失败 {n_fail} / 跳过 {n_skip}")
    verdict = "PASS" if n_fail == 0 else "FAIL"
    print(f"RESULT: {verdict}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
