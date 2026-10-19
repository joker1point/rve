"""占位：Phase 0 不实现音频分析（stems / 说话人 / 转写）。"""

from __future__ import annotations

from ..schema.results import AudioAnalysisResult


async def analyze_audio(
    path: str,
    separate_stems: bool = True,
    diarize: bool = True,
    transcribe: bool = True,
    language: str | None = None,
    device: str = "auto",
) -> AudioAnalysisResult:
    """Phase 0 占位实现。"""
    raise NotImplementedError(
        "analyze_audio 未实现（Phase 2：Demucs/pyannote/faster-whisper）"
    )
