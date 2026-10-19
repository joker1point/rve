"""EditSpec —— RVE 的唯一权威结构（时间锚定，单位秒）。

约定：
- 只存秒（*_sec）；时间码换算只发生在 exporters/ 内，形成单一事实来源。
- 镜头必须连续铺满时间轴（_check_tiling），否则拒绝产出。
- 存疑字段必须显式进入 uncertain[]，不得用默认值蒙混。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    CameraMove,
    CutCurve,
    CutType,
    EvidenceKind,
    ShotSize,
    SpeedMode,
    StemKind,
    SubtitleSource,
)

# 时间容差（秒）：低于此差异视为同一点
EPS = 1e-3
# 时间轴铺满容差（秒）：末镜终点与源时长允许的偏差
TAIL_TOL = 0.05

Score = Annotated[float, Field(ge=0.0, le=1.0)]
Sec = Annotated[float, Field(ge=0.0)]


class Strict(BaseModel):
    """所有结构模型的基类：拒绝未知字段，防止 schema 漂移。"""

    model_config = ConfigDict(extra="forbid")


class VideoSource(Strict):
    """源视频元数据（来自 ffprobe）。"""

    path: str
    duration_sec: Annotated[float, Field(gt=0)]
    fps: Annotated[float, Field(gt=0)]
    width: int
    height: int
    rotation: int = 0
    container: str | None = None
    video_codec: str | None = None
    audio_channels: int | None = None
    audio_sample_rate: int | None = None
    has_audio: bool = True

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height else 0.0


class EvidenceRef(Strict):
    """结论的原始证据引用（抽帧/频谱/日志/上游 JSON）。"""

    kind: EvidenceKind
    path: str
    note: str | None = None


class Provenance(Strict):
    """产出该结论的工具与参数，用于复核与复现。"""

    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    ran_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int | None = None


class Transition(Strict):
    """渐变类过渡的参数（硬切时为空）。"""

    type: CutType
    duration_sec: Sec = 0.0
    direction: str | None = None
    confidence: Score = 0.0


class ShotConfidence(Strict):
    boundary: Score = 0.0
    cut_type: Score = 0.0
    shot_size: Score = 0.0
    camera_move: Score = 0.0
    description: Score = 0.0
    overall: Score = 0.0


class Shot(Strict):
    """单个镜头。in_sec/out_sec 为绝对时间，单位秒。"""

    index: Annotated[int, Field(ge=0)]
    in_sec: Sec
    out_sec: Sec
    cut_type: CutType = CutType.UNKNOWN
    transition: Transition | None = None
    speed: Annotated[float, Field(gt=0)] = 1.0
    speed_mode: SpeedMode = SpeedMode.NORMAL
    shot_size: ShotSize = ShotSize.UNKNOWN
    camera_move: CameraMove = CameraMove.UNKNOWN
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    ocr_text: list[str] = Field(default_factory=list)
    keyframe_path: str | None = None
    confidence: ShotConfidence = Field(default_factory=ShotConfidence)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        return self.out_sec - self.in_sec

    @model_validator(mode="after")
    def _check_order(self) -> Shot:
        if self.out_sec - self.in_sec <= EPS:
            raise ValueError(
                f"shot[{self.index}] 时长非正: {self.in_sec} -> {self.out_sec}"
            )
        return self


class SpeakerSegment(Strict):
    speaker_id: str
    in_sec: Sec
    out_sec: Sec
    text: str | None = None


class Speaker(Strict):
    id: str
    label: str | None = None
    segments: list[SpeakerSegment] = Field(default_factory=list)


class Stem(Strict):
    """分离出的音源（人声/BGM/音效）。"""

    kind: StemKind
    path: str
    duration_sec: Annotated[float, Field(gt=0)]


class AudioInfo(Strict):
    stems: list[Stem] = Field(default_factory=list)
    speakers: list[Speaker] = Field(default_factory=list)
    beats: list[Sec] = Field(default_factory=list)
    beat_sync_ratio: Score | None = None
    has_music: bool = False
    has_speech: bool = False


class Subtitle(Strict):
    in_sec: Sec
    out_sec: Sec
    text: str
    speaker_id: str | None = None
    source: SubtitleSource = SubtitleSource.ASR
    style_hint: str | None = None


class Rhythm(Strict):
    """节奏统计：C 型用于人工复核"是否还原了原片节奏"。"""

    shot_count: Annotated[int, Field(ge=0)] = 0
    avg_shot_sec: Sec = 0.0
    median_shot_sec: Sec = 0.0
    shortest_shot_sec: Sec = 0.0
    longest_shot_sec: Sec = 0.0
    cuts_per_min: Sec = 0.0
    cut_curve: CutCurve = CutCurve.UNKNOWN


class UncertainItem(Strict):
    """存疑字段的显式建模：不静默取单一读数。"""

    field_path: str
    reason: str
    candidates: list[str] = Field(default_factory=list)


class EditSpec(Strict):
    """唯一权威结构。校验失败即拒绝产出。"""

    schema_version: str = "0.1"
    spec_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: VideoSource
    shots: list[Shot] = Field(default_factory=list)
    audio: AudioInfo = Field(default_factory=AudioInfo)
    subtitles: list[Subtitle] = Field(default_factory=list)
    rhythm: Rhythm = Field(default_factory=Rhythm)
    global_style: dict[str, Any] = Field(default_factory=dict)
    uncertain: list[UncertainItem] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_tiling(self) -> EditSpec:
        """核心不变量：镜头连续、无重叠、铺满整条时间轴。

        这是"导出后 NLE 里出现空洞或重叠"的根因闸门——必须先在此处拦截。
        """
        ordered = sorted(self.shots, key=lambda s: s.in_sec)
        prev_out: float | None = None
        for s in ordered:
            if prev_out is not None and abs(s.in_sec - prev_out) > EPS:
                raise ValueError(
                    f"shots 不连续: shot[{s.index}] 起点 {s.in_sec} != 上一镜终点 {prev_out}"
                )
            prev_out = s.out_sec
        if ordered and abs(ordered[-1].out_sec - self.source.duration_sec) > TAIL_TOL:
            raise ValueError(
                f"shots 未铺满时间轴: 末镜终点 {ordered[-1].out_sec} "
                f"!= 源时长 {self.source.duration_sec}"
            )
        return self

    def mark_uncertain(
        self, field_path: str, reason: str, candidates: list[str] | None = None
    ) -> None:
        self.uncertain.append(
            UncertainItem(
                field_path=field_path, reason=reason, candidates=candidates or []
            )
        )
