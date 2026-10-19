"""RVE 契约层：EditSpec 权威结构 + 受控词表 + 工具返回模型。

此目录在 Wave 0 之后视为冻结契约：
- 只允许新增可选字段（带默认值），不得改字段名/类型/语义。
- 所有模型 extra="forbid"，任何未声明字段一律抛错（防接口漂移）。
"""

from .editspec import (
    AudioInfo,
    EditSpec,
    EvidenceRef,
    Provenance,
    Rhythm,
    Shot,
    ShotConfidence,
    Speaker,
    SpeakerSegment,
    Stem,
    Strict,
    Subtitle,
    Transition,
    UncertainItem,
    VideoSource,
)
from .results import (
    AudioAnalysisResult,
    BuildSpecResult,
    ExportResult,
    ShotDescriptionResult,
    ShotDetectionResult,
    ToolReceipt,
    VideoProbeResult,
)

__all__ = [
    "Strict",
    "VideoSource",
    "Shot",
    "ShotConfidence",
    "Transition",
    "EvidenceRef",
    "Provenance",
    "Speaker",
    "SpeakerSegment",
    "Stem",
    "AudioInfo",
    "Subtitle",
    "Rhythm",
    "UncertainItem",
    "EditSpec",
    "ToolReceipt",
    "VideoProbeResult",
    "ShotDetectionResult",
    "AudioAnalysisResult",
    "ShotDescriptionResult",
    "BuildSpecResult",
    "ExportResult",
]
