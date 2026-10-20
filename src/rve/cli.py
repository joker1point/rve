"""RVE 命令行入口（argparse 实现）。

设计要点：
- 与 MCP 工具面共用同一套 pipeline 能力，一条命令装好后既能被 Agent 调用、也能手跑。
- pipeline/workspace/showcases 兄弟模块全部延迟导入：本模块必须始终可 import，
  `rve doctor` 即使依赖全缺也要正常退出（退出码 0，只报告结果）。
- 报错不吞：NotImplementedError -> 退出码 2（人话提示）；其他异常 -> 退出码 1。
  绝不 quietly succeed —— 失败必须可见。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 未实现能力（Phase 2 待补）时的提示语
_NOT_IMPLEMENTED_HINT = "该能力尚未实现（Phase 2）"


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def _probe_command(cmd: str, version_flag: str = "-version") -> str | None:
    """探测外部命令是否可用；可用时返回其版本行，否则返回 None。

    version_flag 允许不同工具有各自的版本参数（ffmpeg 系用 -version，hypit 用 --version）。
    """
    exe = shutil.which(cmd)
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, version_flag],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 — 自检环节任何异常都只算"不可用"
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    return text[0] if text else "(版本未知)"


def _probe_import(module: str) -> tuple[bool, str]:
    """尝试 import 一个模块；返回 (是否可用, 附加说明)。"""
    try:
        mod = __import__(module)
    except Exception as exc:  # noqa: BLE001 — 自检环节任何异常都只算"不可用"
        return False, f"{type(exc).__name__}: {exc}"
    version = getattr(mod, "__version__", "")
    return True, str(version) if version else "已就绪"


def _workspaces_root() -> Path:
    """spec 工作区根目录：优先环境变量 RVE_WORKSPACES，缺省 ~/.rve/workspaces。"""
    env = os.environ.get("RVE_WORKSPACES")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".rve" / "workspaces"


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """探测并打印本地环境：外部命令 / Python 依赖 / 工作区根目录。

    无论探测结果如何都以退出码 0 正常结束（doctor 是"体检报告"，不是"断言"）。
    """
    print("RVE doctor —— 本地环境自检")
    print("-" * 60)

    root = _workspaces_root()
    origin = "环境变量 RVE_WORKSPACES" if os.environ.get("RVE_WORKSPACES") else "默认值"
    print(f"spec 工作区根目录: {root}  (来源: {origin})")
    print("-" * 60)

    results: list[tuple[str, bool, str, str]] = []

    # 外部命令：ffprobe / ffmpeg
    for cmd, advice in (
        ("ffprobe", "安装 ffmpeg（含 ffprobe）并加入 PATH：https://ffmpeg.org/download.html"),
        ("ffmpeg", "同上，ffmpeg 与 ffprobe 通常一起安装"),
    ):
        detail = _probe_command(cmd)
        results.append((cmd, detail is not None, detail or "未找到可执行文件", advice))

    # 可选的第三切镜引擎：hypit（npm 全局包 @hypit/hypit）
    # 缺失只报告、不失败（doctor 是体检报告）；真要用它时 analyze 会显式报错并给安装提示。
    hypit_detail = _probe_command("hypit", "--version")
    results.append(
        (
            "hypit (可选第三切镜引擎)",
            hypit_detail is not None,
            hypit_detail or "未找到可执行文件",
            "安装 hypit（npm i -g @hypit/hypit）以启用可选的第三切镜引擎",
        )
    )

    # Python 依赖
    for module, label, advice in (
        (
            "scenedetect",
            "scenedetect",
            "pip install 'scenedetect>=0.7'（切镜引擎需要）",
        ),
        (
            "opentimelineio",
            "opentimelineio",
            "pip install 'opentimelineio>=0.17'（OTIO 导出需要）",
        ),
        (
            "mcp",
            "mcp",
            "pip install 'mcp>=1.2'（MCP Server 对外暴露需要）",
        ),
    ):
        ok, detail = _probe_import(module)
        results.append((f"{label} (import {module})", ok, detail, advice))

    ok_count = 0
    for name, ok, detail, advice in results:
        if ok:
            ok_count += 1
            print(f"[OK]      {name}  -> {detail}")
        else:
            print(f"[MISSING] {name}  -> {detail}")
            print(f"          建议: {advice}")

    print("-" * 60)
    print(f"自检完成：{ok_count}/{len(results)} 项可用。缺失项不影响 doctor 运行。")
    return 0


# --------------------------------------------------------------------------- #
# pipeline 命令（async）
# --------------------------------------------------------------------------- #


async def _cmd_analyze(args: argparse.Namespace) -> int:
    """一条龙：probe -> detect -> build -> export(fcp7xml)。"""
    from rve.pipeline.build import build_edit_spec
    from rve.pipeline.detect import detect_shots
    from rve.pipeline.export import export_timeline
    from rve.pipeline.probe import probe_video
    from rve.schema.enums import DetectEngine, ExportFormat
    from rve.schema.results import AudioAnalysisResult, ToolReceipt

    engine = DetectEngine(args.engine)
    probe = await probe_video(args.video)
    shots = await detect_shots(
        args.video, engine, None, 0.0, None, extra_engines=args.extra_engine or None
    )

    # Phase 0：audio 能力未实现，按约定传空实例（不调用 analyze_audio）
    audio = AudioAnalysisResult(receipt=ToolReceipt(tool="noop"))

    built = await build_edit_spec(probe, shots, audio, descriptions=None, spec_id=None)

    out_path = None
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / f"{built.spec_id}.fcp7xml.xml")

    exported = await export_timeline(
        built.spec_id, ExportFormat.FCP7XML, out_path, args.fps
    )

    print(f"spec_id: {built.spec_id}")
    print(f"产物: {exported.out_path}")
    return 0


# 命令行 format 取值 -> 受控词表
_EXPORT_FORMATS = {"fcp7xml", "edl", "otio"}


async def _cmd_export(args: argparse.Namespace) -> int:
    """按 spec_id 导出时间线。"""
    from rve.pipeline.export import export_timeline
    from rve.schema.enums import ExportFormat

    fmt = ExportFormat(args.format)
    result = await export_timeline(args.spec_id, fmt, args.out, args.fps)
    print(f"产物: {result.out_path}")
    print(f"格式: {result.format}")
    return 0


async def _cmd_report(args: argparse.Namespace) -> int:
    """按 spec_id 渲染复核报告。"""
    from rve.pipeline.report import render_report

    result = await render_report(args.spec_id, args.format)
    print(f"产物: {result.out_path}")
    print(f"格式: {result.format}")
    return 0


async def _cmd_specs(_args: argparse.Namespace) -> int:
    """列出工作区中已有的 spec_id。"""
    from rve.workspace import list_specs

    ids = list_specs()
    if inspect.isawaitable(ids):
        ids = await ids

    ids = list(ids or [])
    if not ids:
        print("（工作区中暂无已保存的 spec）")
        return 0
    for spec_id in ids:
        print(spec_id)
    return 0


# --------------------------------------------------------------------------- #
# showcase（案例库）
# --------------------------------------------------------------------------- #


async def _cmd_showcase_list(_args: argparse.Namespace) -> int:
    """列出案例库中的全部案例：id / 标题 / 类型 / 是否需要外部素材。"""
    from rve.showcases.registry import list_cases_with_warnings

    cases, warnings = list_cases_with_warnings()
    for warning in warnings:
        print(f"[WARN] {warning}", file=sys.stderr)

    if not cases:
        print("（案例库为空：没有可运行的案例）")
        return 0

    print(f"共 {len(cases)} 个案例：")
    print("-" * 72)
    for case in cases:
        kind = case.material.kind
        needs_external = "是" if kind == "external" else "否"
        print(
            f"{case.id:<20} {case.title:<26} "
            f"类型={kind:<10} 需要外部素材={needs_external}"
        )
    return 0


async def _cmd_showcase_run(args: argparse.Namespace) -> int:
    """跑一个案例：逐项打印「期望 vs 实际」，末尾给结论与退出码。"""
    from rve.showcases.runner import run_case

    workdir = Path(args.workdir) if args.workdir else None
    result = await run_case(
        args.case_id, workdir=workdir, material_override=args.material
    )

    print(f"案例: {result.case_id}")
    print("-" * 72)
    for check in result.checks:
        mark = "OK  " if check.passed else "FAIL"
        print(f"[{mark}] {check.name}: 期望 {check.expected} / 实际 {check.actual}")
    for artifact in result.artifacts:
        print(f"产物: {artifact}")

    # 素材缺失：把「怎么准备」打到 stderr，退出码 2（区别于校验不通过的 1）
    if result.status == "material_missing":
        print(result.message, file=sys.stderr)
        return 2
    if not result.ok:
        print(result.message, file=sys.stderr)
        return 1
    print(result.message)
    return 0


# --------------------------------------------------------------------------- #
# 分发与入口
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器（子命令：doctor/analyze/export/report/specs/showcase）。"""
    parser = argparse.ArgumentParser(
        prog="rve",
        description="RVE — 从成片逆向还原出可导入 NLE 的编辑时间线。",
        # RawDescription：epilog 原样输出，避免长示例被折行拆断
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  rve analyze <video> --engine hypit"
            "            # 只用 hypit 切镜（需本机已装 @hypit/hypit）\n"
            "  rve analyze <video> --extra-engine hypit"
            "       # content+adaptive+hypit 三方交叉验证"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="探测本地依赖与工作区（只报告，不修改）")
    p_doctor.set_defaults(handler=_cmd_doctor)

    p_analyze = sub.add_parser(
        "analyze", help="一条龙：探测 -> 切镜 -> 建 spec -> 导出 fcp7xml"
    )
    p_analyze.add_argument("video", help="输入视频路径")
    p_analyze.add_argument("--fps", type=float, default=None, help="覆盖导出帧率")
    p_analyze.add_argument("--out", default=None, help="产物输出目录")
    p_analyze.add_argument(
        "--engine",
        choices=["auto", "content", "adaptive", "hypit"],
        default="auto",
        help="切镜引擎（默认 auto）。hypit 是可选外部引擎，本机未装 hypit 时会显式报错",
    )
    p_analyze.add_argument(
        "--extra-engine",
        action="append",
        choices=["hypit"],
        default=None,
        help=(
            "额外参与交叉验证的可选外部引擎（可重复传入）。"
            "hypit 为可选依赖，本机未安装时会显式报错并给出安装提示，绝不静默回退"
        ),
    )
    p_analyze.set_defaults(handler=_cmd_analyze)

    p_export = sub.add_parser("export", help="把 spec 导出为 NLE 时间线")
    p_export.add_argument("spec_id", help="目标 spec id")
    p_export.add_argument(
        "--format",
        choices=sorted(_EXPORT_FORMATS),
        default="fcp7xml",
        help="导出格式（默认 fcp7xml）",
    )
    p_export.add_argument("--out", default=None, help="输出文件路径")
    p_export.add_argument("--fps", type=float, default=None, help="覆盖时间线帧率")
    p_export.set_defaults(handler=_cmd_export)

    p_report = sub.add_parser("report", help="渲染复核报告")
    p_report.add_argument("spec_id", help="目标 spec id")
    p_report.add_argument(
        "--format",
        choices=["html", "json"],
        default="html",
        help="报告格式（默认 html）",
    )
    p_report.set_defaults(handler=_cmd_report)

    p_specs = sub.add_parser("specs", help="列出工作区已有的 spec_id")
    p_specs.set_defaults(handler=_cmd_specs)

    # showcase：案例库（一级子命令 + 二级动作 list / run）
    p_showcase = sub.add_parser(
        "showcase", help="案例库：列出/运行可复现的案例配方"
    )
    showcase_sub = p_showcase.add_subparsers(dest="showcase_action", required=True)

    p_sc_list = showcase_sub.add_parser("list", help="列出全部案例")
    p_sc_list.set_defaults(handler=_cmd_showcase_list)

    p_sc_run = showcase_sub.add_parser("run", help="跑一个案例并逐项校验期望指标")
    p_sc_run.add_argument("case_id", help="目标案例 id")
    p_sc_run.add_argument(
        "--workdir", default=None, help="运行产物目录（默认系统临时目录）"
    )
    p_sc_run.add_argument(
        "--material", default=None, help="外部素材的本地文件路径"
    )
    p_sc_run.set_defaults(handler=_cmd_showcase_run)

    return parser


def _run(coro) -> int:
    """执行 async 处理函数并统一映射退出码。

    NotImplementedError -> 2（人话提示，Phase 2 待补）
    其他异常            -> 1（简短信息，绝不假装成功）
    """
    try:
        return asyncio.run(coro)
    except NotImplementedError:
        print(_NOT_IMPLEMENTED_HINT, file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — 顶层兜底，错误必须可见
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数并分派到对应子命令，返回进程退出码。"""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 的 --help/参数错误走这里
        return exc.code if isinstance(exc.code, int) else 1

    handler = args.handler
    try:
        if inspect.iscoroutinefunction(handler):
            return _run(handler(args))
        return handler(args)
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
