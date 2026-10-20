"""hypit 可选第三切镜引擎的**对外工具面**测试（CLI + MCP）。

范围只覆盖本模块负责的面：参数解析、doctor 探测、MCP 工具 schema。
切镜流水线本身的行为由 tests/test_detect_cross.py 负责，这里不重复。

原则：
- hypit 是**可选**依赖：装了就报 [OK]，没装就报 [MISSING]，doctor 仍退出 0；
- 参数面必须把 hypit 与 --extra-engine 露出来（装不装得上都得看得见）；
- MCP 工具面必须带 extra_engines 参数，且 schema 合法。
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from rve import cli
from rve.cli import build_parser, main


# --------------------------------------------------------------------------- #
# CLI：参数面
# --------------------------------------------------------------------------- #


def test_root_help_mentions_hypit_and_extra_engine(
    capsys: pytest.CaptureFixture,
) -> None:
    """`rve --help` 要能看到 hypit 与 --extra-engine（可选引擎必须可发现）。"""
    code = main(["--help"])
    out = capsys.readouterr().out
    assert code == 0
    assert "hypit" in out, "`rve --help` 未提到 hypit"
    assert "--extra-engine" in out, "`rve --help` 未提到 --extra-engine"


def test_analyze_help_lists_hypit_and_extra_engine(
    capsys: pytest.CaptureFixture,
) -> None:
    """`rve analyze --help`：--engine 的取值含 hypit，且有 --extra-engine 选项。"""
    code = main(["analyze", "--help"])
    out = capsys.readouterr().out
    assert code == 0
    assert "hypit" in out, "`rve analyze --help` 未提到 hypit"
    assert "--extra-engine" in out, "`rve analyze --help` 缺 --extra-engine 选项"


def test_engine_choices_accept_hypit() -> None:
    """--engine 的取值集合含 hypit（且不破坏原有取值）。"""
    args = build_parser().parse_args(["analyze", "v.mp4", "--engine", "hypit"])
    assert args.engine == "hypit"

    args = build_parser().parse_args(["analyze", "v.mp4", "--engine", "auto"])
    assert args.engine == "auto"


def test_extra_engine_is_appendable_and_defaults_to_none() -> None:
    """--extra-engine 可重复传入（append），默认 None（不改变默认行为）。"""
    parser = build_parser()

    assert parser.parse_args(["analyze", "v.mp4"]).extra_engine is None

    args = parser.parse_args(["analyze", "v.mp4", "--extra-engine", "hypit"])
    assert args.extra_engine == ["hypit"]

    args = parser.parse_args(
        ["analyze", "v.mp4", "--extra-engine", "hypit", "--extra-engine", "hypit"]
    )
    assert args.extra_engine == ["hypit", "hypit"]


def test_extra_engine_rejects_unknown_engine() -> None:
    """未登记的额外引擎必须在 argparse 层就被拒（不得悄悄忽略）。"""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["analyze", "v.mp4", "--extra-engine", "nope"])
    assert excinfo.value.code != 0


# --------------------------------------------------------------------------- #
# CLI：doctor 探测
# --------------------------------------------------------------------------- #


def test_doctor_reports_hypit_and_exits_zero(
    capsys: pytest.CaptureFixture,
) -> None:
    """doctor 必须提到 hypit，且退出码恒为 0（体检报告不是断言）。

    装没装 hypit 都算通过：断言输出里有 "hypit" 这个词即可。
    """
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0, "`rve doctor` 退出码必须为 0"
    assert "hypit" in out, "`rve doctor` 输出未提到 hypit"


def test_doctor_missing_hypit_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """本机没装 hypit 时 doctor 仍退出 0，并给出安装提示（只报告，不失败）。"""

    real_which = cli.shutil.which

    def fake_which(name, *args, **kwargs):
        if name == "hypit":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(cli.shutil, "which", fake_which)

    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0, "缺 hypit 时 doctor 仍须退出 0"
    assert "hypit" in out
    assert "[MISSING]" in out, "缺 hypit 时应报 [MISSING] 而不是静默跳过"
    assert "npm" in out.lower(), "缺 hypit 时应给出安装提示"


# --------------------------------------------------------------------------- #
# CLI：analyze 的参数透传
# --------------------------------------------------------------------------- #


def test_analyze_forwards_extra_engines_to_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """`--extra-engine hypit` 必须真的透传给 pipeline.detect_shots。

    桩掉整条流水线，只断言"参数有没有送到"——这是本模块的接线责任。
    """
    import rve.pipeline.build as build_mod
    import rve.pipeline.detect as detect_mod
    import rve.pipeline.export as export_mod
    import rve.pipeline.probe as probe_mod

    seen: dict = {}

    async def fake_detect(path, *args, **kwargs):
        seen["path"] = path
        seen["args"] = args
        seen["kwargs"] = kwargs
        return SimpleNamespace()

    async def fake_probe(path):
        return SimpleNamespace()

    async def fake_build(*args, **kwargs):
        return SimpleNamespace(spec_id="stub-spec")

    async def fake_export(*args, **kwargs):
        return SimpleNamespace(out_path="stub-out")

    monkeypatch.setattr(detect_mod, "detect_shots", fake_detect)
    monkeypatch.setattr(probe_mod, "probe_video", fake_probe)
    monkeypatch.setattr(build_mod, "build_edit_spec", fake_build)
    monkeypatch.setattr(export_mod, "export_timeline", fake_export)

    code = main(["analyze", "v.mp4", "--extra-engine", "hypit"])
    capsys.readouterr()
    assert code == 0
    assert seen.get("kwargs", {}).get("extra_engines") == ["hypit"], (
        f"--extra-engine 未透传给 detect_shots: {seen}"
    )


def test_analyze_without_extra_engine_passes_none(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """不带 --extra-engine 时透传 None（默认行为与改动前完全一致）。"""
    import rve.pipeline.build as build_mod
    import rve.pipeline.detect as detect_mod
    import rve.pipeline.export as export_mod
    import rve.pipeline.probe as probe_mod

    seen: dict = {}

    async def fake_detect(path, *args, **kwargs):
        seen["kwargs"] = kwargs
        return SimpleNamespace()

    async def fake_probe(path):
        return SimpleNamespace()

    async def fake_build(*args, **kwargs):
        return SimpleNamespace(spec_id="stub-spec")

    async def fake_export(*args, **kwargs):
        return SimpleNamespace(out_path="stub-out")

    monkeypatch.setattr(detect_mod, "detect_shots", fake_detect)
    monkeypatch.setattr(probe_mod, "probe_video", fake_probe)
    monkeypatch.setattr(build_mod, "build_edit_spec", fake_build)
    monkeypatch.setattr(export_mod, "export_timeline", fake_export)

    code = main(["analyze", "v.mp4"])
    capsys.readouterr()
    assert code == 0
    assert seen.get("kwargs", {}).get("extra_engines") is None


# --------------------------------------------------------------------------- #
# MCP 工具面
# --------------------------------------------------------------------------- #


def _tool_schema(tool) -> dict:
    """取工具的 input schema（mcp 1.x 叫 inputSchema，2.x 改名 input_schema）。"""
    for name in ("inputSchema", "input_schema"):
        schema = getattr(tool, name, None)
        if schema is not None:
            return schema
    raise AssertionError(f"工具 {tool.name} 没有 input schema")


def test_mcp_detect_shots_schema_has_extra_engines() -> None:
    """MCP 的 detect_shots 工具必须带 extra_engines 参数，且 schema 合法。"""
    pytest.importorskip("mcp")

    from rve.mcp.server import build_server

    server = build_server()
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}
    assert "detect_shots" in by_name, f"缺 detect_shots 工具: {sorted(by_name)}"

    schema = _tool_schema(by_name["detect_shots"])
    assert isinstance(schema, dict)
    assert schema.get("type") == "object", f"schema type 非 object: {schema.get('type')!r}"
    properties = schema.get("properties")
    assert isinstance(properties, dict), "schema 缺 properties 或 properties 非 dict"
    assert "extra_engines" in properties, (
        f"detect_shots 的 input schema 缺 extra_engines: {sorted(properties)}"
    )


def test_mcp_tool_count_unchanged() -> None:
    """接 hypit **不新增** MCP 工具：工具名与数量保持不变。"""
    pytest.importorskip("mcp")

    from rve.mcp.server import build_server

    tools = asyncio.run(build_server().list_tools())
    names = {t.name for t in tools}
    assert len(tools) == 10, f"工具数量变了: {len(tools)}"
    assert {
        "probe_video",
        "detect_shots",
        "analyze_audio",
        "describe_shots",
        "build_edit_spec",
        "get_edit_spec",
        "export_timeline",
        "render_report",
        "list_showcases",
        "run_showcase",
    } == names


def test_mcp_detect_shots_docstring_mentions_hypit_contract() -> None:
    """docstring 是给 Agent 读的接口说明：必须写清 hypit 的语义与失败行为。"""
    from rve.mcp.server import detect_shots as tool_fn

    doc = inspect.getdoc(tool_fn) or ""
    assert "hypit" in doc, "docstring 未提到 hypit"
    assert "extra_engines" in doc, "docstring 未提到 extra_engines"
    assert "disagreements" in doc, "docstring 未说明分歧去处"
    assert "绝不静默回退" in doc, "docstring 未说明缺失时不静默回退"


# --------------------------------------------------------------------------- #
# 冻结契约：pipeline 侧是否已就位
# --------------------------------------------------------------------------- #


def test_pipeline_detect_shots_supports_extra_engines() -> None:
    """pipeline.detect_shots 必须接受 extra_engines（冻结契约）。

    若主线程尚未落地该参数，本用例 skip 并说明原因——不能为了让它变绿
    而去改 pipeline/enums（那两处不在本任务的改动白名单内）。
    """
    from rve.pipeline.detect import detect_shots as pipeline_detect

    params = inspect.signature(pipeline_detect).parameters
    if "extra_engines" not in params:
        pytest.skip("依赖未到位：pipeline.detect_shots 尚未提供 extra_engines 参数")
    assert params["extra_engines"].default is None


def test_detect_engine_enum_has_hypit() -> None:
    """DetectEngine 须有 HYPIT 取值（冻结契约）。"""
    from rve.schema.enums import DetectEngine

    if "HYPIT" not in DetectEngine.__members__:
        pytest.skip("依赖未到位：DetectEngine 尚未加入 HYPIT")
    assert DetectEngine.HYPIT.value == "hypit"


# --------------------------------------------------------------------------- #
# 回归保护：既有命令不受影响
# --------------------------------------------------------------------------- #


def test_showcase_list_still_works(capsys: pytest.CaptureFixture) -> None:
    """`rve showcase list` 不受本次改动影响（回归保护）。"""
    code = main(["showcase", "list"])
    out = capsys.readouterr().out
    assert code == 0, f"`rve showcase list` 退出码 {code}"
    assert out.strip(), "`rve showcase list` 没有任何输出"


def test_unknown_subcommand_still_nonzero() -> None:
    """未知子命令仍返回非 0（不得 quietly succeed）。"""
    assert main(["definitely-not-a-command"]) != 0
