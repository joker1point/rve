"""占位：Phase 0 不实现逐镜描述（OCR / VLM）。"""

from __future__ import annotations

from ..schema.enums import VlmProvider
from ..schema.results import ShotDescriptionResult


async def describe_shots(
    spec_id: str,
    provider: VlmProvider = VlmProvider.OFF,
    sample: str = "diff",
    ocr: bool = True,
) -> ShotDescriptionResult:
    """Phase 0 占位实现。"""
    raise NotImplementedError("describe_shots 未实现（Phase 2：OCR/VLM）")
