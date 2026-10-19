"""导出器包：把 EditSpec 导出为可被 NLE 导入的时间线格式。

- `fcp7`：纯标准库实现的 FCP7 XML（xmeml），永远可用、永远可测；
- `otio_export`：基于 opentimelineio 的 OTIO 导出，**延迟导入** otio，
  故此处不在包级 import，避免把 otio 变成硬依赖。
"""

from __future__ import annotations

from .fcp7 import to_fcp7_xml, write_fcp7_xml

__all__ = ["to_fcp7_xml", "write_fcp7_xml"]
