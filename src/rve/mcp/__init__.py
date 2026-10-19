"""RVE 的 MCP 工具面（对 AI Agent 暴露的门面）。

本包故意保持"空壳"：不在导入期拉起 server.py。
原因：server.py 会导入 `mcp` 包，而 pipeline/workspace 等兄弟模块可能尚未就位；
若在此处急切 import，`import rve.cli` 会连带失败，破坏外壳层的可用性。

因此统一走 __getattr__ 惰性导出：
    from rve.mcp import build_server   # 只有真正用到时才加载 server.py
"""

from __future__ import annotations

__all__ = ["build_server", "run_stdio"]


def __getattr__(name: str):
    if name in __all__:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
