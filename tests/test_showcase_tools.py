"""案例库（showcases）的对外工具面与 CLI 冒烟测试。

只验证"对外契约"，不重复核心层（src/rve/showcases/）自己的单测：
- MCP 工具面：`list_showcases` / `run_showcase` 必须注册，且 input schema 合法；
- CLI：`showcase` 子命令存在、`showcase list` 退出码 0 且有输出；
- CLI：`showcase run <不存在的id>` 必须非 0 退出（错误不得被吞掉）。

MCP 工具面依赖 `mcp` 包，未安装则整段 skip，而不是 failure。
"""

from __future__ import annotations

import asyncio

import pytest

from rve.cli import main

# 期望出现在 MCP 工具面上的案例库工具名
SHOWCASE_TOOLS = ("list_showcases", "run_showcase")


# --------------------------------------------------------------------------- #
# MCP 工具面
# --------------------------------------------------------------------------- #


def _tool_schema(tool: object) -> dict:
    """取工具的 input schema。

    mcp 1.x 叫 inputSchema、2.x 改名 input_schema —— 两种都读，
    免得"开发机装 1.x 绿、CI/用户全新装 2.x 红"。
    """
    for name in ("inputSchema", "input_schema"):
        value = getattr(tool, name, None)
        if value is not None:
            return value
    return {}


def test_mcp_server_registers_showcase_tools() -> None:
    """MCP 工具面必须注册两个案例库工具。"""
    pytest.importorskip("mcp")

    from rve.mcp.server import build_server

    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    missing = set(SHOWCASE_TOOLS) - names
    assert not missing, f"MCP 工具面缺少案例库工具: {sorted(missing)}"


def test_mcp_showcase_tools_have_valid_input_schema() -> None:
    """两个案例库工具的 input schema 必须是合法的 object（闸门 S6 同款判据）。"""
    pytest.importorskip("mcp")

    from rve.mcp.server import build_server

    server = build_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    for name in SHOWCASE_TOOLS:
        assert name in tools, f"工具 {name} 未注册"
        schema = _tool_schema(tools[name])
        assert isinstance(schema, dict), f"工具 {name} 的 input schema 非 dict"
        assert schema.get("type") == "object", (
            f"工具 {name} 的 input schema type != 'object'（{schema.get('type')!r}）"
        )
        properties = schema.get("properties")
        assert isinstance(properties, dict), f"工具 {name} 的 input schema 缺 properties"

    # run_showcase 的必填参数：案例 id（list_showcases 无参，故不要求有 required）
    run_schema = _tool_schema(tools["run_showcase"])
    run_props = run_schema.get("properties") or {}
    assert "case_id" in run_props, f"run_showcase 缺 case_id 参数: {sorted(run_props)}"
    assert "case_id" in (run_schema.get("required") or [])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_help_contains_showcase(capsys: pytest.CaptureFixture) -> None:
    """`rve --help` 必须能看到 showcase 子命令。

    argparse 的 --help 走 SystemExit，被 main() 捕获后返回退出码 0。
    """
    code = main(["--help"])
    out = capsys.readouterr().out
    assert code == 0
    assert "showcase" in out


def test_cli_showcase_list_exits_zero_with_output(
    capsys: pytest.CaptureFixture,
) -> None:
    """`rve showcase list` 退出码 0 且有输出（案例库不为空）。"""
    code = main(["showcase", "list"])
    captured = capsys.readouterr()
    assert code == 0, f"`rve showcase list` 退出码 {code}: {captured.err}"
    assert captured.out.strip(), "`rve showcase list` 没有任何输出"


def test_cli_showcase_run_unknown_case_returns_nonzero() -> None:
    """跑不存在的案例 id 必须非 0 退出（错误可见，不得 quietly succeed）。"""
    code = main(["showcase", "run", "definitely-not-a-showcase-case"])
    assert code != 0, "不存在的案例 id 必须返回非 0 退出码"
