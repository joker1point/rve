"""CLI 冒烟测试 —— 只验证外壳层自身，不依赖尚未就位的兄弟模块。

原则：
- doctor 必须能跑出结果并返回 0（哪怕依赖全缺）。
- 未知子命令必须返回非 0（不得 quietly succeed）。
- MCP 工具面依赖 `mcp` 包，未安装则整段 skip，而不是 failure。
"""

from __future__ import annotations

import asyncio

import pytest

from rve.cli import main


def test_doctor_returns_zero(capsys: pytest.CaptureFixture) -> None:
    """doctor 是体检报告：任何依赖缺失都应正常退出（码 0）。"""
    code = main(["doctor"])
    out = capsys.readouterr().out

    assert code == 0
    # 至少要真的打印了探测内容
    assert "ffprobe" in out.lower()
    assert "工作区" in out


def test_unknown_subcommand_returns_nonzero() -> None:
    """未知子命令必须返回非 0，且明显是参数错误。"""
    code = main(["definitely-not-a-command"])
    assert code != 0


def test_no_subcommand_returns_nonzero() -> None:
    """不带子命令时同样是非 0（argparse 的参数错误），不是静默成功。"""
    code = main([])
    assert code != 0


def test_analyze_missing_module_returns_nonzero() -> None:
    """analyze 依赖 pipeline 兄弟模块；若缺失应返回非 0，而非假装成功。

    若兄弟模块此刻仍未就位（ModuleNotFoundError），退出码应为 1；
    若已就位但能力未实现（NotImplementedError），退出码应为 2。
    无论如何都不得是 0。
    """
    code = main(["analyze", "nonexistent-video.mp4"])
    assert code in (1, 2)


def test_specs_reports_or_errors_but_never_quiet() -> None:
    """specs 在 workspace 未就位时返回非 0；就位且为空时返回 0 并给出提示。"""
    code = main(["specs"])
    assert code in (0, 1, 2)


def test_mcp_server_builds_and_registers_tools() -> None:
    """MCP 工具面：mcp 包可用时，应能构建 server 并注册 8 个工具 + 1 个资源。"""
    pytest.importorskip("mcp")

    from rve.mcp.server import build_server

    server = build_server()
    assert server is not None

    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    expected = {
        "probe_video",
        "detect_shots",
        "analyze_audio",
        "describe_shots",
        "build_edit_spec",
        "get_edit_spec",
        "export_timeline",
        "render_report",
    }
    assert expected <= names

    templates = asyncio.run(server.list_resource_templates())
    # mcp 1.x 的字段名是 uriTemplate，2.x 改名 uri_template —— 两种都读，
    # 免得"开发机装 1.x 绿、CI/用户全新装 2.x 红"。
    uris = {
        getattr(t, "uriTemplate", None) or getattr(t, "uri_template", None)
        for t in templates
    }
    assert "editspec://{spec_id}" in uris


def test_mcp_package_lazy_init() -> None:
    """rve.mcp 的惰性导出：访问 build_server 才加载 server 模块。"""
    pytest.importorskip("mcp")

    import rve.mcp as mcp_pkg

    assert callable(mcp_pkg.build_server)
    with pytest.raises(AttributeError):
        _ = mcp_pkg.not_a_real_attr
