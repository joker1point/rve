"""案例库（showcases）的数据模型。

设计要点：
- 一个案例 = 一份**配方**：素材从哪来、怎么准备、期望指标是什么。
- 期望指标（expected）是**可校验**的数值：对不上就是失败，绝不 quietly succeed。
- 模型全部继承 schema.editspec.Strict（extra="forbid"）：字段漂移必须显式暴露。
"""

from __future__ import annotations

from typing import Any

from rve.schema.editspec import Strict
from rve.schema.results import ToolReceipt


class MaterialSpec(Strict):
    """案例素材的来源与准备方式。

    kind：
        "synthetic" —— 程序合成素材（ffmpeg 现场生成，零版权、可入仓、可 CI 跑）。
        "external"  —— 外部公开素材（素材本身不入库，只存来源、许可与准备命令）。
    prepare：
        synthetic 时是**可执行**的 ffmpeg 命令行（`{{out}}` 会被替换为输出文件路径）；
        external 时是**给人看**的下载 + 截取命令（不会被自动执行）。
    """

    kind: str
    license: str
    source: str  # 合成素材填 "programmatic"；外部素材填下载 URL
    prepare: list[str]  # 人类可读的生成/获取命令
    local_hint: str = ""  # 外部素材的本地放置指引
    duration_sec: float | None = None
    fps: float | None = None


class ShowcaseCase(Strict):
    """一个可复现的案例配方。"""

    id: str
    title: str
    summary: str
    material: MaterialSpec
    expected: dict[str, Any]  # 期望指标，见 runner 的校验口径
    tags: list[str] = []
    requires_network: bool = False


class CheckResult(Strict):
    """单项期望指标的校验结果（期望值与实测值都留字符串，便于直接展示）。"""

    name: str
    passed: bool
    expected: str
    actual: str


class ShowcaseListResult(Strict):
    """列出案例的结果。"""

    cases: list[ShowcaseCase]
    receipt: ToolReceipt


class ShowcaseRunResult(Strict):
    """跑一个案例的结果。

    status：
        "pass"             —— 全部期望指标通过；
        "fail"             —— 跑通了但指标对不上；
        "material_missing" —— 素材没准备好（消息里写清怎么准备）；
        "error"            —— 流程执行出错（消息里带异常类型与原文）。
    """

    case_id: str
    ok: bool
    status: str
    checks: list[CheckResult] = []
    artifacts: list[str] = []
    message: str = ""
    receipt: ToolReceipt
