"""RVE 案例库（showcases）：把"跑出来是什么样"变成可复现、可校验的工程资产。

模块划分：
- models   ：案例配方的数据模型；
- registry ：读取包内 cases/<case_id>/case.json；
- material ：素材准备（程序合成 / 外部素材，**绝不联网下载**）；
- runner   ：跑通一个案例并校验产出是否与期望一致。

约定：本包**延迟导入** pipeline 相关模块，import 本包不会拖垮缺依赖的环境。
"""

from .material import MaterialMissing, ensure_material
from .models import (
    CheckResult,
    MaterialSpec,
    ShowcaseCase,
    ShowcaseListResult,
    ShowcaseRunResult,
)
from .registry import list_cases, list_cases_with_warnings, load_case
from .runner import run_case

__all__ = [
    "CheckResult",
    "MaterialMissing",
    "MaterialSpec",
    "ShowcaseCase",
    "ShowcaseListResult",
    "ShowcaseRunResult",
    "ensure_material",
    "list_cases",
    "list_cases_with_warnings",
    "load_case",
    "run_case",
]
