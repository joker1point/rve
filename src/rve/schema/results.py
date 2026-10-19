"""各 MCP 工具的返回模型。每个返回都带一份可复核的 receipt。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .editspec import (
    AudioInfo,
    EditSpec,
    Shot,
    Strict,
    UncertainItem,
    VideoSource,
)


class ToolReceipt(Strict):
    """工具调用回执：参数、耗时、产物、告警，全部可回溯。"""

    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    started_at: datetime = Field(default_factory=datetime.now)
    duration_ms: int = 0
    artifacts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VideoProbeResult(Strict):
    source: VideoSource
    receipt: ToolReceipt


class ShotDetectionResult(Strict):
    path: str
    engine_used: str
    shots: list[Shot] = Field(default_factory=list)
    # 双引擎分歧：一致处采信，分歧处必须显式暴露，不得静默取其一
    disagreements: list[UncertainItem] = Field(default_factory=list)
    receipt: ToolReceipt


class AudioAnalysisResult(Strict):
    audio: AudioInfo = Field(default_factory=AudioInfo)
    transcript: list[Any] = Field(default_factory=list)
    receipt: ToolReceipt


class ShotDescriptionResult(Strict):
    descriptions: dict[int, str] = Field(default_factory=dict)
    ocr: dict[int, list[str]] = Field(default_factory=dict)
    receipt: ToolReceipt


class BuildSpecResult(Strict):
    spec_id: str
    spec: EditSpec
    receipt: ToolReceipt


class ExportResult(Strict):
    format: str
    out_path: str
    receipt: ToolReceipt
