"""镜头检测步骤：把 PySceneDetect 检测异步化。"""

from __future__ import annotations

import asyncio

from ..providers.shots_pyscenedetect import PySceneDetectShotDetector
from ..schema.enums import DetectEngine
from ..schema.results import ShotDetectionResult


async def detect_shots(
    path: str,
    engine: DetectEngine = DetectEngine.AUTO,
    threshold: float | None = None,
    start_sec: float = 0.0,
    end_sec: float | None = None,
) -> ShotDetectionResult:
    """检测镜头边界；engine=auto 时双引擎交叉验证，engine=transnet 抛 NotImplementedError。"""
    detector = PySceneDetectShotDetector()
    return await asyncio.to_thread(
        detector.detect, path, engine, threshold, start_sec, end_sec
    )
