"""ffprobe 探针实现：解析容器/流元数据，构造 VideoSource。

只负责"读元数据"，不做任何剪辑语义推断。
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime

from ..schema.editspec import VideoSource
from ..schema.results import ToolReceipt, VideoProbeResult


class FfprobeError(RuntimeError):
    """ffprobe 调用或输出解析失败。"""


def _parse_fraction(value: str | None) -> float | None:
    """把 ffprobe 的分数帧率（如 "30000/1001"）换算为浮点；非法或非正返回 None。"""
    if not value:
        return None
    try:
        if "/" in value:
            num_s, den_s = value.split("/", 1)
            den = float(den_s)
            if den == 0:
                return None
            result = float(num_s) / den
        else:
            result = float(value)
    except (ValueError, ZeroDivisionError):
        return None
    return result if result > 0 else None


def _parse_rotation(stream: dict) -> int:
    """读取旋转角：优先 Display Matrix 的 side_data，其次 tags.rotate，最后 0。"""
    rotation: float | None = None
    for side in stream.get("side_data_list") or []:
        if "display matrix" in str(side.get("side_data_type", "")).lower():
            if side.get("rotation") is not None:
                rotation = float(side["rotation"])
                break
    if rotation is None:
        raw = (stream.get("tags") or {}).get("rotate")
        if raw is not None:
            try:
                rotation = float(raw)
            except (TypeError, ValueError):
                rotation = None
    if rotation is None:
        return 0
    return int(round(rotation)) % 360


def _parse_duration(fmt: dict, video_stream: dict) -> float | None:
    """时长优先级：容器 format.duration → 视频流 duration。"""
    for candidate in (fmt.get("duration"), video_stream.get("duration")):
        if candidate is None:
            continue
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def parse_ffprobe_output(data: dict, path: str) -> VideoSource:
    """把 ffprobe JSON 解析为 VideoSource（纯函数，便于单测）。"""
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio_stream = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
    )
    if video_stream is None:
        raise FfprobeError(f"未找到视频流: {path}")

    # fps 优先 avg_frame_rate（真实平均帧率），退化取 r_frame_rate
    fps = _parse_fraction(video_stream.get("avg_frame_rate")) or _parse_fraction(
        video_stream.get("r_frame_rate")
    )
    if fps is None:
        raise FfprobeError(f"无法解析有效帧率: {path}")

    duration = _parse_duration(fmt, video_stream)
    if duration is None:
        raise FfprobeError(f"无法解析有效时长: {path}")

    channels = audio_stream.get("channels") if audio_stream else None
    sample_rate = audio_stream.get("sample_rate") if audio_stream else None

    return VideoSource(
        path=path,
        duration_sec=duration,
        fps=fps,
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        rotation=_parse_rotation(video_stream),
        container=fmt.get("format_name"),
        video_codec=video_stream.get("codec_name"),
        audio_channels=int(channels) if channels is not None else None,
        audio_sample_rate=int(sample_rate) if sample_rate is not None else None,
        has_audio=audio_stream is not None,
    )


def run_ffprobe(path: str) -> dict:
    """执行 ffprobe 并返回解析后的 JSON。"""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:  # ffprobe 不在 PATH
        raise FfprobeError("未找到 ffprobe 可执行文件（需安装 ffmpeg）") from exc
    if proc.returncode != 0:
        raise FfprobeError(
            f"ffprobe 失败(rc={proc.returncode}): {proc.stderr.strip()[:300]}"
        )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FfprobeError(
            f"ffprobe 输出非合法 JSON: {proc.stdout[:200]!r}"
        ) from exc


class FfprobeProvider:
    """基于 ffprobe 的探针实现（满足 ProbeProvider 协议）。"""

    tool_name = "ffprobe"

    def probe(self, path: str) -> VideoProbeResult:
        started = datetime.now()
        t0 = time.perf_counter()
        warnings: list[str] = []

        data = run_ffprobe(path)
        source = parse_ffprobe_output(data, path)
        if not source.has_audio:
            warnings.append("源视频无可识别的音频流")

        receipt = ToolReceipt(
            tool=self.tool_name,
            params={"path": path, "fields": ["format", "streams"]},
            ok=True,
            started_at=started,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            artifacts=[path],
            warnings=warnings,
        )
        return VideoProbeResult(source=source, receipt=receipt)
