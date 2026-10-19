"""RVE 受控词表。所有结构性取值必须来自此处，不允许自由文本充当结构。"""

from enum import StrEnum


class CutType(StrEnum):
    """镜头边界类型（指进入该镜头的过渡）。"""

    HARD = "hard"
    DISSOLVE = "dissolve"
    FADE = "fade"
    WIPE = "wipe"
    SLIDE = "slide"
    ZOOM = "zoom"
    FLASH = "flash"
    UNKNOWN = "unknown"


class ShotSize(StrEnum):
    EXTREME_CLOSE_UP = "extreme_close_up"
    CLOSE_UP = "close_up"
    MEDIUM_CLOSE_UP = "medium_close_up"
    MEDIUM = "medium"
    MEDIUM_WIDE = "medium_wide"
    WIDE = "wide"
    EXTREME_WIDE = "extreme_wide"
    UNKNOWN = "unknown"


class CameraMove(StrEnum):
    STATIC = "static"
    PAN = "pan"
    TILT = "tilt"
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    DOLLY = "dolly"
    HANDHELD = "handheld"
    ORBIT = "orbit"
    UNKNOWN = "unknown"


class SpeedMode(StrEnum):
    NORMAL = "normal"
    SLOW = "slow"
    FAST = "fast"
    RAMP = "speed_ramp"
    UNKNOWN = "unknown"


class StemKind(StrEnum):
    VOCALS = "vocals"
    BGM = "bgm"
    SFX = "sfx"
    OTHER = "other"


class SubtitleSource(StrEnum):
    ASR = "asr"
    OCR = "ocr"
    FUSED = "fused"
    MANUAL = "manual"


class CutCurve(StrEnum):
    FAST_START = "fast_start"
    STEADY = "steady"
    SLOW_BUILD = "slow_build"
    IRREGULAR = "irregular"
    UNKNOWN = "unknown"


class DetectEngine(StrEnum):
    AUTO = "auto"
    CONTENT = "content"
    ADAPTIVE = "adaptive"
    TRANSNET = "transnet"


class VlmProvider(StrEnum):
    OFF = "off"
    LOCAL = "local"
    CLOUD = "cloud"


class ExportFormat(StrEnum):
    OTIO = "otio"
    EDL = "edl"
    FCP7XML = "fcp7xml"
    FCPXML = "fcpxml"
    RESOLVE_XML = "resolve_xml"


class EvidenceKind(StrEnum):
    FRAME = "frame"
    SPECTROGRAM = "spectrogram"
    LOG = "log"
    JSON = "json"
