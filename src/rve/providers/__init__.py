"""Provider 实现集合：探针与镜头检测的可替换后端。"""

from __future__ import annotations

from .base import ProbeProvider, ShotDetector
from .ffprobe import FfprobeError, FfprobeProvider, parse_ffprobe_output, run_ffprobe
from .shots_pyscenedetect import PySceneDetectShotDetector

__all__ = [
    "ProbeProvider",
    "ShotDetector",
    "FfprobeProvider",
    "FfprobeError",
    "run_ffprobe",
    "parse_ffprobe_output",
    "PySceneDetectShotDetector",
]
