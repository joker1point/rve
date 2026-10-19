"""Provider 抽象协议：探针与镜头检测的可替换实现契约。

极简原则：只声明"输入 → 结构化结果"的方法签名，不引入状态与实现细节，
以便 Phase 2 直接替换为其它实现（如 TransNetV2）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schema.enums import DetectEngine
from ..schema.results import ShotDetectionResult, VideoProbeResult


@runtime_checkable
class ProbeProvider(Protocol):
    """视频探针：读取源视频元数据，产出可复核的回执。"""

    def probe(self, path: str) -> VideoProbeResult:
        ...


@runtime_checkable
class ShotDetector(Protocol):
    """镜头检测：把视频切成连续、无重叠、铺满时间轴的镜头。"""

    def detect(
        self,
        path: str,
        engine: DetectEngine = DetectEngine.AUTO,
        threshold: float | None = None,
        start_sec: float = 0.0,
        end_sec: float | None = None,
    ) -> ShotDetectionResult:
        ...
