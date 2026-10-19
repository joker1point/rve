"""探针步骤：把 ffprobe 探针异步化。"""

from __future__ import annotations

import asyncio

from ..providers.ffprobe import FfprobeProvider
from ..schema.results import VideoProbeResult


async def probe_video(path: str) -> VideoProbeResult:
    """读取源视频元数据（ffprobe）。"""
    provider = FfprobeProvider()
    return await asyncio.to_thread(provider.probe, path)
