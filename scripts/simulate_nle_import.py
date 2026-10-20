"""模拟「真人在 NLE 里导入并核对」——独立解析器 + 面板渲染 + 真值比对。

为什么需要它
------------
本项目唯一未闭环的验收项是「真人在 Premiere / Resolve 里导入 FCP7 XML，
打开时间线面板，核对镜头数 / 总时长 / 转场数」。本机没有任何 NLE，
CI 又是 headless，这一步做不了。

于是把它拆成两层：
1. **模拟层（本脚本）**：站在 NLE 的位置，只吃 FCP7 XML 与磁盘上的素材文件，
   按公开 xmeml 约定重建出「时间线面板上应该显示什么」——片段、起止帧、
   转场、素材是否链接得上、音轨。**绝不 import rve.***，也不读 EditSpec：
   它看到的世界和一个 NLE 看到的世界一样多。
2. **真值层**（scripts/make_nle_ground_truth.py）：用 ffmpeg 造一段真值在设计期
   就已知的合成素材（每段画面的帧数、每两个段落的转场类型全部由命令精确决定），
   于是「时间线该长什么样」是先验事实，而不是 RVE 自己的检测结果——
   比对因此不构成循环论证。

人工核对时看的六件事，脚本逐条判定：
  ① 时间线总长      ② 片段数        ③ 每段的起止位置
  ④ 转场的位置与类型 ⑤ 素材是否真的链接得上（媒体离线是导入最常见的失败）
  ⑥ 音轨是否与画面对齐

⚠️ 边界（必须说清楚）：本脚本**不验证 Premiere / Resolve 会不会接受这个 XML**。
它验证的是「XML 的时间线语义能被一个独立实现正确重建，且与素材的设计真值一致」。
真正的 NLE 导入仍需人工在装有 Premiere/Resolve 的机器上执行一次。

用法::

    python scripts/simulate_nle_import.py <timeline.xml> \\
        [--expect ground_truth.json] [--json out.json] [--html out.html]

退出码：0 = 全部核对通过；1 = 有核对项不通过；2 = 用法/文件错误。
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Clip:
    """时间线上的一个片段（视频或音频轨上的一条）。"""

    name: str
    start_frame: int
    end_frame: int
    in_frame: int
    out_frame: int
    duration: int
    media_path: str | None
    media_exists: bool | None

    @property
    def length(self) -> int:
        return self.end_frame - self.start_frame


@dataclass
class Transition:
    """一个转场（xmeml 的 transitionitem）。"""

    name: str
    effect_id: str
    start_frame: int
    end_frame: int
    duration: int


@dataclass
class Timeline:
    """从 FCP7 XML 重建出的时间线。"""

    sequence_name: str
    timebase: int
    ntsc: bool
    fps: float
    sequence_duration_frames: int
    video_clips: list[Clip] = field(default_factory=list)
    audio_clips: list[Clip] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)

    @property
    def timeline_end_frame(self) -> int:
        """时间线实际结束帧：取视频轨最后片段的 end。"""
        if not self.video_clips:
            return 0
        return max(c.end_frame for c in self.video_clips)


@dataclass
class Check:
    """一条核对结论。"""

    name: str
    passed: bool
    expected: str
    actual: str


# --------------------------------------------------------------------------- #
# 解析（只依赖标准库；不 import rve.*）
# --------------------------------------------------------------------------- #
def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _int(node: ET.Element | None, default: int = 0) -> int:
    raw = _text(node)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def parse_rate(node: ET.Element | None) -> tuple[int, bool]:
    """解析 <rate>：返回 (timebase, ntsc)。"""
    if node is None:
        return 0, False
    timebase = _int(node.find("timebase"))
    ntsc = _text(node.find("ntsc")).upper() == "TRUE"
    return timebase, ntsc


def fps_from_rate(timebase: int, ntsc: bool) -> float:
    """由 timebase + ntsc 推真实帧率。

    NTSC 整数帧率比实际帧率高 0.1%（30 ↔ 29.97），故 timebase=30 且 ntsc=TRUE
    对应 30000/1001。**这是 NLE 侧自己的换算**，与 RVE 导出器的换算互不引用。
    """
    if timebase <= 0:
        return 0.0
    return timebase * (1000.0 / 1001.0) if ntsc else float(timebase)


def file_url_to_path(url: str) -> str | None:
    """把 <pathurl> 的 file URL 还原成本地路径；非 file URL 返回 None。

    形如 ``file://localhost/C:/dir/a.mp4``（Windows）或 ``file:///home/u/a.mp4``。
    """
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
        return None
    # Windows：/C:/dir/... -> C:/dir/...
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path or None


def _parse_clipitem(node: ET.Element) -> Clip:
    """解析一个 <clipitem>。"""
    file_node = node.find("file")
    path_url = _text(file_node.find("pathurl")) if file_node is not None else ""
    media_path = file_url_to_path(path_url)
    exists: bool | None = None
    if media_path:
        exists = Path(media_path).is_file()
    return Clip(
        name=_text(node.find("name")),
        start_frame=_int(node.find("start")),
        end_frame=_int(node.find("end")),
        in_frame=_int(node.find("in")),
        out_frame=_int(node.find("out")),
        duration=_int(node.find("duration")),
        media_path=media_path,
        media_exists=exists,
    )


def _parse_transitionitem(node: ET.Element) -> Transition:
    """解析一个 <transitionitem>。"""
    effect = node.find("effect")
    return Transition(
        name=_text(node.find("name")),
        effect_id=_text(effect.find("effectid")) if effect is not None else "",
        start_frame=_int(node.find("start")),
        end_frame=_int(node.find("end")),
        duration=_int(node.find("duration")),
    )


def load_timeline(xml_path: str | Path) -> Timeline:
    """从 FCP7 XML 重建时间线。文件不存在/格式不对会抛异常（不静默返回空壳）。"""
    path = Path(xml_path)
    if not path.is_file():
        raise FileNotFoundError(f"时间线文件不存在: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"时间线 XML 解析失败: {exc}") from exc

    if root.tag != "xmeml":
        raise ValueError(f"根标签不是 <xmeml>，实为 <{root.tag}>")

    sequence = root.find("sequence")
    if sequence is None:
        raise ValueError("缺少 <sequence>")

    timebase, ntsc = parse_rate(sequence.find("rate"))
    media = sequence.find("media")
    video_track = media.find("video/track") if media is not None else None
    audio_track = media.find("audio/track") if media is not None else None

    timeline = Timeline(
        sequence_name=_text(sequence.find("name")),
        timebase=timebase,
        ntsc=ntsc,
        fps=fps_from_rate(timebase, ntsc),
        sequence_duration_frames=_int(sequence.find("duration")),
    )
    if video_track is not None:
        for node in video_track:
            if node.tag == "clipitem":
                timeline.video_clips.append(_parse_clipitem(node))
            elif node.tag == "transitionitem":
                timeline.transitions.append(_parse_transitionitem(node))
    if audio_track is not None:
        for node in audio_track:
            if node.tag == "clipitem":
                timeline.audio_clips.append(_parse_clipitem(node))

    timeline.video_clips.sort(key=lambda c: c.start_frame)
    timeline.transitions.sort(key=lambda t: t.start_frame)
    return timeline


# --------------------------------------------------------------------------- #
# 结构自检（不依赖真值，永远执行）
# --------------------------------------------------------------------------- #
def structural_checks(tl: Timeline) -> list[Check]:
    """时间线自身的完整性：连续、无重叠、时长自洽、转场跨骑、素材可链接。"""
    checks: list[Check] = []
    clips = tl.video_clips

    # ① 片段首尾相接（无空洞、无重叠）—— 导入后最直观的"对不上"
    gaps: list[str] = []
    for prev, cur in zip(clips, clips[1:]):
        if cur.start_frame != prev.end_frame:
            kind = "空洞" if cur.start_frame > prev.end_frame else "重叠"
            gaps.append(f"{prev.name}→{cur.name} {kind} {abs(cur.start_frame - prev.end_frame)} 帧")
    checks.append(
        Check(
            name="片段首尾相接",
            passed=not gaps,
            expected="各片段 end == 下一片段 start",
            actual="；".join(gaps) if gaps else f"{len(clips)} 个片段连续无空洞无重叠",
        )
    )

    # ② 序列时长与片段总长一致
    seq_frames = tl.sequence_duration_frames
    end_frames = tl.timeline_end_frame
    checks.append(
        Check(
            name="序列时长自洽",
            passed=seq_frames == end_frames,
            expected=f"sequence duration = {seq_frames} 帧",
            actual=f"片段铺满到 {end_frames} 帧",
        )
    )

    # ③ 转场必须跨骑在剪切点上（start < 切点 < end）
    cut_points = {prev.end_frame for prev, prev_next in zip(clips, clips[1:])}
    bad: list[str] = []
    for tr in tl.transitions:
        if not any(tr.start_frame < cut < tr.end_frame for cut in cut_points):
            bad.append(
                f"{tr.name or tr.effect_id} [{tr.start_frame},{tr.end_frame}] 不跨骑任何剪切点"
            )
    checks.append(
        Check(
            name="转场跨骑剪切点",
            passed=not bad,
            expected="每个转场跨骑在某个剪切点上",
            actual="；".join(bad) if bad else f"{len(tl.transitions)} 个转场全部跨骑",
        )
    )

    # ④ 素材是否真的能链接上（媒体离线 = 导入失败的头号原因）
    offline = [
        f"{c.name} → {c.media_path}"
        for c in clips
        if c.media_path is None or not c.media_exists
    ]
    checks.append(
        Check(
            name="素材链接可用",
            passed=not offline,
            expected="每个片段的 <file> 都指向磁盘上真实存在的文件",
            actual="；".join(offline) if offline else f"{len(clips)} 个片段素材全部在线",
        )
    )

    return checks


# --------------------------------------------------------------------------- #
# 与设计期真值比对
# --------------------------------------------------------------------------- #
def _transition_kind(tr: Transition) -> str:
    """把转场归类成 dissolve / fade（按 xmeml 的 name 与 effectid 判定）。"""
    token = f"{tr.name} {tr.effect_id}".lower()
    if "fade" in token and "cross" not in token:
        return "fade"
    if "cross" in token or "dissolve" in token:
        return "dissolve"
    return "unknown"


def compare_truth(tl: Timeline, truth: dict) -> list[Check]:
    """把重建出的时间线与设计期真值逐项比对（真值来自素材本身，与 RVE 无关）。"""
    checks: list[Check] = []
    tol = truth.get("tolerances", {})
    dur_tol = float(tol.get("duration_sec", 0.05))
    cut_tol = float(tol.get("hard_cut_sec", 0.1))
    win_tol = float(tol.get("transition_window_sec", 0.5))
    fps = tl.fps or 30.0

    # ① 时间线总长
    want_frames = int(truth.get("total_frames", 0))
    got_frames = tl.timeline_end_frame
    want_sec = float(truth.get("total_duration_sec", 0.0))
    got_sec = got_frames / fps
    checks.append(
        Check(
            name="时间线总长",
            passed=abs(got_sec - want_sec) <= dur_tol,
            expected=f"{want_sec:.3f}s（{want_frames} 帧）",
            actual=f"{got_sec:.3f}s（{got_frames} 帧）",
        )
    )

    # ② 片段数：纯硬切切出「硬切数 + 1」镜；两处渐变各可能额外切一刀 → 判区间
    lo = int(truth.get("shot_count_min", 0))
    hi = int(truth.get("shot_count_max", 0))
    got_count = len(tl.video_clips)
    checks.append(
        Check(
            name="片段数",
            passed=lo <= got_count <= hi,
            expected=f"{lo}~{hi} 个（硬切 {lo - 1} 处，渐变区归属浮动）",
            actual=f"{got_count} 个",
        )
    )

    # ③ 硬切点：每个设计的硬切点都必须出现在重建出的时间线边界上。
    #    渐变区可能额外切一刀，所以不比对「镜头起点」，只比对硬切点——
    #    后者是素材侧不容争辩的事实，前者取决于渐变区归属的约定。
    boundaries = [c.start_frame / fps for c in tl.video_clips]
    detail: list[str] = []
    hard_cuts = [float(x) for x in truth.get("hard_cuts_sec", [])]
    ok = True
    for want in hard_cuts:
        if not any(abs(b - want) <= cut_tol for b in boundaries):
            ok = False
            detail.append(f"硬切点 {want:.3f}s 未在时间线上找到")
    checks.append(
        Check(
            name="硬切点位置",
            passed=ok,
            expected=f"{len(hard_cuts)} 处硬切（{', '.join(f'{x:.3f}s' for x in hard_cuts)}），容差 ±{cut_tol}s",
            actual="；".join(detail) if detail else "全部命中",
        )
    )

    # ④ 转场：每个设计的转场时间窗，都要有一个同类型的 transitionitem，
    #    且它跨骑的剪切点落在该时间窗内（窗口外扩 win_tol 允许判据差异）
    clip_cuts = sorted({c.end_frame for c in tl.video_clips[:-1]})

    def crossed_cut(tr: Transition) -> int | None:
        """转场跨骑在哪个剪切点上（剪切点 = 相邻片段的交界）。"""
        for cut in clip_cuts:
            if tr.start_frame < cut < tr.end_frame:
                return cut
        return None

    want_trs = truth.get("transitions", [])
    tr_detail: list[str] = []
    tr_ok = len(tl.transitions) == len(want_trs)
    if not tr_ok:
        tr_detail.append(f"数量 期望 {len(want_trs)} 处 / 实测 {len(tl.transitions)} 处")
    for want in want_trs:
        span = [float(x) for x in want.get("span_sec", [0.0, 0.0])]
        hit = None
        for tr in tl.transitions:
            if _transition_kind(tr) != want["type"]:
                continue
            cut = crossed_cut(tr)
            if cut is None:
                continue
            at = cut / fps
            if span[0] - win_tol <= at <= span[1] + win_tol:
                hit = at
                break
        if hit is None:
            tr_ok = False
            tr_detail.append(
                f"{want['type']} 未落在设计窗口 {span[0]:.3f}~{span[1]:.3f}s 内"
            )
    checks.append(
        Check(
            name="转场类型与时间窗",
            passed=tr_ok,
            expected=f"{len(want_trs)} 处（{', '.join(t['type'] for t in want_trs)}），窗口外扩 ±{win_tol}s",
            actual="；".join(tr_detail)
            if tr_detail
            else f"{len(tl.transitions)} 处均落在设计窗口内",
        )
    )

    # ⑤ 音轨是否与画面对齐（音频片段数应与视频片段数一致）
    if truth.get("expect_audio"):
        want_audio = len(tl.video_clips)
        got_audio = len(tl.audio_clips)
        checks.append(
            Check(
                name="音轨对齐",
                passed=got_audio == want_audio and got_audio > 0,
                expected=f"{want_audio} 个音频片段（与视频片段数一致）",
                actual=f"{got_audio} 个音频片段",
            )
        )

    return checks


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _timecode(frame: int, fps: float) -> str:
    """帧号 → HH:MM:SS:FF。"""
    tb = int(round(fps)) or 1
    total_sec = frame // tb
    return f"{total_sec // 3600:02d}:{(total_sec // 60) % 60:02d}:{total_sec % 60:02d}:{frame % tb:02d}"


def render_text(tl: Timeline, checks: list[Check], truth: dict | None) -> str:
    """人读报告：模拟「打开时间线面板后看到什么」。"""
    lines: list[str] = []
    lines.append("模拟 NLE 导入 —— 时间线面板视图")
    lines.append("=" * 72)
    lines.append(
        f"序列: {tl.sequence_name or '(未命名)'} | 帧率: {tl.fps:.3f} fps "
        f"(timebase={tl.timebase} ntsc={'TRUE' if tl.ntsc else 'FALSE'})"
    )
    lines.append(
        f"时间线总长: {tl.timeline_end_frame} 帧 = "
        f"{tl.timeline_end_frame / tl.fps if tl.fps else 0:.3f}s "
        f"（sequence duration {tl.sequence_duration_frames} 帧）"
    )
    lines.append(f"片段数: {len(tl.video_clips)} | 转场: {len(tl.transitions)} | 音频片段: {len(tl.audio_clips)}")
    lines.append("-" * 72)
    lines.append(f"{'#':>3} {'片段名':<14} {'序列入点':>12} {'序列出点':>12} {'时长':>9}  素材")
    for idx, clip in enumerate(tl.video_clips, start=1):
        media = "在线" if clip.media_exists else ("离线" if clip.media_path else "无引用")
        lines.append(
            f"{idx:>3} {clip.name:<14} "
            f"{_timecode(clip.start_frame, tl.fps):>12} "
            f"{_timecode(clip.end_frame, tl.fps):>12} "
            f"{clip.length / tl.fps if tl.fps else 0:>8.3f}s  {media}"
        )
    if tl.transitions:
        lines.append("-" * 72)
        lines.append("转场:")
        for tr in tl.transitions:
            lines.append(
                f"  {tr.name or tr.effect_id:<16} "
                f"{_timecode(tr.start_frame, tl.fps)} → {_timecode(tr.end_frame, tl.fps)} "
                f"({tr.duration} 帧)"
            )
    lines.append("-" * 72)
    lines.append("核对结论:")
    for check in checks:
        mark = "PASS" if check.passed else "FAIL"
        lines.append(f"  [{mark}] {check.name}: 期望 {check.expected} / 实测 {check.actual}")
    passed = sum(1 for c in checks if c.passed)
    lines.append("=" * 72)
    lines.append(f"通过 {passed} / 失败 {len(checks) - passed}")
    lines.append("裁决: " + ("PASS —— 时间线与素材设计真值一致" if passed == len(checks) else "FAIL"))
    if truth is None:
        lines.append("（未提供真值文件，本次只做结构自检）")
    return "\n".join(lines)


def render_html(tl: Timeline, checks: list[Check], truth: dict | None) -> str:
    """单文件 HTML：复刻「时间线面板」，让人能一眼核对。"""
    total = max(1, tl.timeline_end_frame)
    fps = tl.fps or 30.0
    colors = ["#0FDC78", "#80C1FF", "#FFB86B", "#C792EA", "#7FD1AE", "#F87454"]

    def pct(frames: int) -> float:
        return frames / total * 100.0

    clip_html: list[str] = []
    for idx, clip in enumerate(tl.video_clips, start=1):
        color = colors[(idx - 1) % len(colors)]
        width = pct(clip.length)
        clip_html.append(
            f'<div class="clip" style="width:{width:.4f}%;background:{color}" '
            f'title="{clip.name}">'
            f'<span class="cno">{idx}</span>'
            f'<span class="ctc">{_timecode(clip.start_frame, fps)}</span>'
            f'<span class="cdur">{clip.length / fps:.2f}s</span>'
            f"</div>"
        )

    trans_html: list[str] = []
    for tr in tl.transitions:
        left = pct(tr.start_frame)
        width = max(0.6, pct(tr.end_frame - tr.start_frame))
        trans_html.append(
            f'<div class="trans" style="left:{left:.4f}%;width:{width:.4f}%" '
            f'title="{tr.name or tr.effect_id}"></div>'
        )

    ruler = []
    seconds = int(total / fps) + 1
    for sec in range(seconds):
        left = pct(min(sec * fps, total))
        ruler.append(
            f'<div class="tick" style="left:{left:.4f}%"><span>{_timecode(int(sec * fps), fps)}</span></div>'
        )

    rows = []
    for check in checks:
        cls = "ok" if check.passed else "bad"
        mark = "PASS" if check.passed else "FAIL"
        rows.append(
            f'<tr class="{cls}"><td class="mark">{mark}</td><td>{check.name}</td>'
            f'<td class="exp">{check.expected}</td><td class="act">{check.actual}</td></tr>'
        )

    truth_block = ""
    if truth:
        rows_html = []
        for cut in truth.get("hard_cuts_sec", []):
            rows_html.append(
                f'<tr><td>硬切</td><td>内容突变</td><td colspan="2">{cut:.3f}s</td></tr>'
            )
        for tr in truth.get("transitions", []):
            span = tr.get("span_sec", [0.0, 0.0])
            rows_html.append(
                f'<tr><td>{tr["type"]}</td><td>渐变时间窗</td>'
                f'<td colspan="2">{span[0]:.3f}s ~ {span[1]:.3f}s</td></tr>'
            )
        for seg in truth.get("segments", []):
            rows_html.append(
                f'<tr><td>素材段 {seg["index"] + 1}</td><td>{seg["label"]}</td>'
                f'<td colspan="2">{seg["start_sec"]:.3f}s ~ {seg["end_sec"]:.3f}s</td></tr>'
            )
        truth_block = (
            '<h2>素材设计期真值（与 RVE 的检测结果无关）</h2>'
            '<table class="truth"><tr><th>事件</th><th>内容</th><th colspan="2">位置</th></tr>'
            + "".join(rows_html)
            + "</table>"
        )

    passed = sum(1 for c in checks if c.passed)
    verdict = "PASS" if passed == len(checks) else "FAIL"
    passed_all = "ok" if verdict == "PASS" else "bad"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>RVE 模拟 NLE 导入 · 时间线面板</title>
<style>
 :root {{ color-scheme: dark; --bg:#0D1117; --panel:#161B22; --line:#30363D;
   --text:#E6EDF3; --muted:#8B949E; --ok:#3FB950; --bad:#F85149; }}
 * {{ box-sizing: border-box; }}
 body {{ margin:0; padding:24px; background:var(--bg); color:var(--text);
   font:14px/1.6 "PingFang SC","Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif; }}
 h1 {{ font-size:20px; margin:0 0 4px; }}
 h2 {{ font-size:15px; margin:28px 0 10px; color:var(--muted); font-weight:600; }}
 .meta {{ color:var(--muted); font-size:13px; margin-bottom:18px; }}
 .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px 18px; }}
 .track-label {{ font-size:12px; color:var(--muted); margin:0 0 6px; }}
 .track {{ position:relative; display:flex; height:54px; background:#0d1117;
   border:1px solid var(--line); border-radius:6px; overflow:hidden; }}
 .clip {{ position:relative; border-right:1px solid rgba(0,0,0,.45); color:#04210f;
   display:flex; flex-direction:column; justify-content:center; align-items:center;
   overflow:hidden; min-width:0; font-weight:600; }}
 .clip .cno {{ font-size:13px; line-height:1.1; }}
 .clip .ctc {{ font-size:10px; opacity:.85; font-weight:500; }}
 .clip .cdur {{ font-size:10px; opacity:.85; font-weight:500; }}
 .trans {{ position:absolute; top:0; height:100%; background:repeating-linear-gradient(
   45deg, rgba(255,255,255,.35) 0 4px, rgba(255,255,255,.12) 4px 8px);
   border-left:1px solid #fff; border-right:1px solid #fff; pointer-events:none; }}
 .ruler {{ position:relative; height:20px; margin-bottom:6px; }}
 .tick {{ position:absolute; top:0; border-left:1px solid var(--line); padding-left:3px;
   font-size:10px; color:var(--muted); }}
 table {{ border-collapse:collapse; width:100%; margin-top:8px; }}
 th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); font-size:13px; }}
 th {{ color:var(--muted); font-weight:600; }}
 td.mark {{ font-weight:700; }}
 tr.ok td.mark {{ color:var(--ok); }}
 tr.bad td.mark {{ color:var(--bad); }}
 tr.bad td.exp {{ color:var(--bad); }}
 .verdict {{ display:inline-block; padding:4px 12px; border-radius:6px; font-weight:700; }}
 .verdict.ok {{ background:rgba(63,185,80,.16); color:var(--ok); }}
 .verdict.bad {{ background:rgba(248,81,73,.16); color:var(--bad); }}
 .note {{ color:var(--muted); font-size:12px; margin-top:14px; }}
</style></head><body>
<h1>RVE · 模拟 NLE 导入（时间线面板视图）</h1>
<div class="meta">序列 <b>{tl.sequence_name or "(未命名)"}</b> · 帧率 {fps:.3f} fps
 (timebase={tl.timebase}, ntsc={'TRUE' if tl.ntsc else 'FALSE'}) ·
 片段 {len(tl.video_clips)} · 转场 {len(tl.transitions)} · 音频片段 {len(tl.audio_clips)} ·
 总长 {tl.timeline_end_frame} 帧 = {tl.timeline_end_frame / fps:.3f}s</div>

<div class="panel">
  <p class="track-label">V1 视频轨</p>
  <div class="ruler">{"".join(ruler)}</div>
  <div class="track">{"".join(clip_html)}{"".join(trans_html)}</div>
  <p class="track-label" style="margin-top:14px">A1 音频轨</p>
  <div class="track">{"".join(f'<div class="clip" style="width:{pct(c.length):.4f}%;background:#8B949E"></div>' for c in tl.audio_clips)}</div>
</div>

<h2>核对结论（模拟真人在面板上核对的六件事）</h2>
<table><tr><th>结果</th><th>核对项</th><th>期望</th><th>实测</th></tr>{"".join(rows)}</table>
<p style="margin-top:14px">裁决: <span class="verdict {passed_all}">{verdict}（{passed}/{len(checks)}）</span></p>
{truth_block}
<p class="note">本面板由 <code>scripts/simulate_nle_import.py</code> 从 FCP7 XML 独立重建
（不 import <code>rve.*</code>、不读 EditSpec）；真值来自
<code>scripts/make_nle_ground_truth.py</code> 的素材设计。
它验证 XML 的时间线语义，<b>不等于</b> Premiere / Resolve 一定接受该 XML。</p>
</body></html>"""


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="模拟真人在 NLE 里导入 FCP7 XML 并核对时间线"
    )
    parser.add_argument("timeline", help="FCP7 XML 时间线文件")
    parser.add_argument(
        "--expect", default=None, help="ground_truth.json（比对基准）"
    )
    parser.add_argument("--json", dest="json_out", default=None, help="写出 JSON 报告")
    parser.add_argument("--html", dest="html_out", default=None, help="写出 HTML 面板")
    args = parser.parse_args(argv)

    try:
        tl = load_timeline(args.timeline)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    truth = None
    if args.expect:
        truth_path = Path(args.expect)
        if not truth_path.is_file():
            print(f"错误：真值文件不存在: {truth_path}", file=sys.stderr)
            return 2
        truth = json.loads(truth_path.read_text(encoding="utf-8"))

    checks = structural_checks(tl)
    if truth is not None:
        checks += compare_truth(tl, truth)

    print(render_text(tl, checks, truth))

    if args.json_out:
        payload = {
            "sequence": tl.sequence_name,
            "fps": tl.fps,
            "timebase": tl.timebase,
            "ntsc": tl.ntsc,
            "sequence_duration_frames": tl.sequence_duration_frames,
            "timeline_end_frame": tl.timeline_end_frame,
            "video_clips": [c.__dict__ for c in tl.video_clips],
            "audio_clips": [c.__dict__ for c in tl.audio_clips],
            "transitions": [t.__dict__ for t in tl.transitions],
            "checks": [c.__dict__ for c in checks],
            "verdict": "PASS" if all(c.passed for c in checks) else "FAIL",
        }
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"JSON 报告: {out}")

    if args.html_out:
        out = Path(args.html_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(tl, checks, truth), encoding="utf-8")
        print(f"HTML 面板: {out}")

    return 0 if all(c.passed for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
