#!/usr/bin/env python
"""独立校验器 —— 读回 FCP7 XML（xmeml）并与 EditSpec 逐镜比对。

为什么"独立"
------------
RVE 的交付闸门（``scripts/acceptance.py``）里，写 XML 的代码回头断言这份 XML 合法，
属于**自证**。本模块是第二实现：它 **只依赖 Python 标准库**，且**绝不 import 任何
``rve.*``**（一旦依赖被验对象就不再独立）。它按公开的 FCP7 XML（xmeml）约定
**从第一性原理重建序列时间轴**：

  * 对每个 video ``clipitem`` 读 ``<start>/<end>``（序列位置）与 ``<in>/<out>``（素材内源帧），
    建立「源帧 → 序列帧」的映射；
  * 与独立加载的 ``spec.json``（EditSpec）逐镜比对：每镜 start/duration、时间轴空洞/重叠、
    sequence ``duration`` 是否等于末镜 ``end``、``transitionitem`` 是否严格跨骑剪切点、
    序列是否带 ``<timecode>``、音频轨是否与源 ``has_audio`` 一致。

用法::

    python scripts/independent_verify.py <xmeml 文件> [--spec <spec.json>] [--json]

任何不一致 → 非零退出（退出码 1）；全部成立 → 退出码 0 并打印裁决 PASS。

无 ``--spec`` 时为「结构模式」：只解析 XML 并报告读取器所见（供真实样本对标用），
仅当**解析失败 / 存在无可辩驳的结构自相矛盾**时才判失败。带 ``--spec`` 时为
「RVE 契约模式」：执行上面全部断言。

帧号解析说明（来自真实产物对标的坑）
------------------------------------
真实 NLE（Premiere / FCP7）导出的 xmeml 里，``<start>/<end>`` 可能是 **-1**（未设哨兵），
帧号也可能是**浮点**（实测 ``<out>1055.0``），``<clipitem><rate>`` 可与序列帧率不同，
且单条轨内部**允许空洞**、可从非 0 开始。本读取器对这些形态一律容忍：负数哨兵视作
"未设"，数字按 ``float`` 解析，多轨按启发式选"主轨"。这些容忍只在解析层，不影响
RVE 契约模式下对自家产物的严格断言。
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# NTSC 非整数帧率（与公开约定一致；仅用于核对 <ntsc> 标记）
_NTSC_RATES = (23.976, 29.97, 59.94)
_NTSC_TOL = 0.01
_FRAME_TOL = 0.5  # 帧级比较容差：真实产物可能出现 1055.0 这类浮点帧号


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _localname(tag: str) -> str:
    """去掉 XML namespace，只留本地名（兼容带 namespace 的写法）。"""
    return tag.rsplit("}", 1)[-1]


def _num(text: str | None) -> float | None:
    """文本 → float；负数（真实产物里的 -1 哨兵）视作"未设"；坏值视作 None。"""
    if text is None:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _rate_block(el: ET.Element | None) -> tuple[float | None, str | None]:
    """从元素里取 ``<rate>``：(timebase, ntsc)。"""
    if el is None:
        return None, None
    rate = next((c for c in el if _localname(c.tag) == "rate"), None)
    if rate is None:
        return None, None
    tb = next((c.text for c in rate if _localname(c.tag) == "timebase"), None)
    ntsc = next((c.text for c in rate if _localname(c.tag) == "ntsc"), None)
    return (_num(tb), ntsc.strip().upper() if ntsc else None)


def _direct(el: ET.Element, name: str) -> ET.Element | None:
    """取直接子元素（按本地名，顺序无关——真实产物子元素顺序不统一）。"""
    return next((c for c in el if _localname(c.tag) == name), None)


def _text(el: ET.Element | None, name: str) -> str | None:
    child = _direct(el, name) if el is not None else None
    return child.text if child is not None else None


class Clip:
    """一个 video/audio clipitem 的关键时间字段（帧号，float 或 None）。"""

    __slots__ = ("cid", "name", "start", "end", "cin", "cout", "duration",
                 "clip_timebase", "has_file")

    def __init__(self, el: ET.Element) -> None:
        self.cid = el.get("id")
        self.name = _text(el, "name")
        self.start = _num(_text(el, "start"))
        self.end = _num(_text(el, "end"))
        self.cin = _num(_text(el, "in"))
        self.cout = _num(_text(el, "out"))
        self.duration = _num(_text(el, "duration"))
        self.clip_timebase, _ = _rate_block(el)
        self.has_file = _direct(el, "file") is not None


class Track:
    """一条 <track>：clipitems（按文档序）与 transitionitems。"""

    __slots__ = ("index", "clips", "transitions")

    def __init__(self, index: int, el: ET.Element) -> None:
        self.index = index
        self.clips = [Clip(c) for c in el if _localname(c.tag) == "clipitem"]
        self.transitions = [
            (_num(_text(c, "start")), _num(_text(c, "end")))
            for c in el if _localname(c.tag) == "transitionitem"
        ]


class Parsed:
    """一次解析的完整结果。"""

    def __init__(self) -> None:
        self.root_tag = ""
        self.version: str | None = None
        self.doctype = False
        self.duration: float | None = None
        self.seq_timebase: float | None = None
        self.seq_ntsc: str | None = None
        self.has_timecode = False
        self.video_tracks: list[Track] = []
        self.audio_tracks: list[Track] = []
        self.primary: Track | None = None


def parse_xmeml(path: Path) -> Parsed:
    """解析 xmeml 文件为 Parsed 结构（纯标准库，容忍真实产物形态）。"""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    p = Parsed()
    p.doctype = raw.lstrip().startswith("<?xml") and "<!DOCTYPE" in raw[:400]
    root = ET.fromstring(raw)
    p.root_tag = _localname(root.tag)
    p.version = root.get("version")

    seq = next((c for c in root if _localname(c.tag) == "sequence"), None)
    if seq is None:
        return p
    p.duration = _num(_text(seq, "duration"))
    p.seq_timebase, p.seq_ntsc = _rate_block(seq)
    p.has_timecode = _direct(seq, "timecode") is not None

    media = _direct(seq, "media")
    if media is not None:
        video = _direct(media, "video")
        if video is not None:
            p.video_tracks = [
                Track(i, t) for i, t in enumerate(video)
                if _localname(t.tag) == "track"
            ]
        audio = _direct(media, "audio")
        if audio is not None:
            p.audio_tracks = [
                Track(i, t) for i, t in enumerate(audio)
                if _localname(t.tag) == "track"
            ]

    p.primary, _ = choose_primary_track(p.video_tracks, p.duration)
    return p


def _spans(clips: list[Clip]) -> list[tuple[float, float]]:
    """可用的 (start, end) 序列片段（跳过 start/end 未设的 clip）。"""
    return [(c.start, c.end) for c in clips if c.start is not None and c.end is not None]


def choose_primary_track(
    tracks: list[Track], duration: float | None
) -> tuple[Track | None, str]:
    """挑"主轨"（供时间轴重建）：优先"从 0 起、连续铺满整条序列"的轨。

    真实多轨产物里，主内容不一定是文档序第一条轨（可能有空轨、overlay 轨）。
    返回 (主轨, 选择理由)。
    """
    candidates = [t for t in tracks if _spans(t.clips)]
    if not candidates:
        return None, "无可用 video clipitem"

    def tiles(t: Track) -> bool:
        spans = _spans(t.clips)
        if not spans or spans[0][0] != 0:
            return False
        for (_, prev_end), (cur_start, _) in zip(spans, spans[1:]):
            if abs(cur_start - prev_end) > _FRAME_TOL:
                return False
        return duration is None or abs(spans[-1][1] - duration) <= _FRAME_TOL

    for t in candidates:
        if tiles(t):
            return t, f"track#{t.index}（从 0 起连续铺满整条序列）"
    best = max(candidates, key=lambda t: len(t.clips))
    return best, f"track#{best.index}（无整轨铺满者，取 clipitem 最多的一条）"


# --------------------------------------------------------------------------- #
# 重建与比对
# --------------------------------------------------------------------------- #
def reconstruct_timeline(clips: list[Clip]) -> tuple[list[str], list[str], list[str]]:
    """从第一性原理重建序列时间轴，返回 (结论行, 致命问题, 观察项)。

    仅用 XML 内部信息，建立「源帧 → 序列帧」映射并报告其形态。注意：真实 NLE 产物里，
    序列可以**不是**原片的重排（偏移逐镜不同）、单条轨内部**允许空洞**、也可从非 0 开始——
    这些是合法编辑，故仅作观察项；致命判定留给 spec 契约模式（对自家产物才成立）。
    唯一在结构层即判非法的是"时间倒挂"（end < start），这在任何 NLE 里都不可能。
    """
    info: list[str] = []
    problems: list[str] = []
    notes: list[str] = []
    usable = [c for c in clips if c.start is not None and c.end is not None]
    skipped = [c for c in clips if c.start is None or c.end is None]
    if skipped:
        notes.append(f"{len(skipped)} 个 clipitem 的 start/end 未设（-1 哨兵），未参与重建")

    if not usable:
        notes.append("无带 start/end 的 video clipitem，跳过时间轴重建")
        return info, problems, notes

    spans = [(c.start, c.end) for c in usable]
    for c, (s, e) in zip(usable, spans):
        if e < s:
            problems.append(f"clipitem {c.cid}: end {e} < start {s}（时间倒挂）")

    # 源帧 → 序列帧 的映射：同一 clip 内 seq = start + (f - in)，故 start-in 应为常数
    offsets = [(c.cid, c.start - c.cin) for c in usable if c.cin is not None]
    if offsets:
        base = offsets[0][1]
        drift = max(abs(o - base) for _, o in offsets)
        if drift > 2 * _FRAME_TOL + 1:
            info.append(
                f"映射偏移范围: {min(o for _, o in offsets):.3f} ~ {max(o for _, o in offsets):.3f} 帧"
                "（非纯平移 = 成片不是原片的重排；真实产物正常）"
            )
        else:
            info.append(f"「源帧→序列帧」映射为纯平移（偏移 ≈ {base:.3f} 帧，跨 {len(usable)} 镜一致）")

    first_start = spans[0][0]
    info.append(f"重建时间轴: 首个 clipitem start={first_start:g}，末个 end={spans[-1][1]:g}")
    gaps = [
        i for i, ((_, prev_end), (cur_start, _)) in enumerate(zip(spans, spans[1:]))
        if abs(cur_start - prev_end) > _FRAME_TOL
    ]
    if not gaps:
        info.append("时间轴连续性: 主轨逐镜首尾相接，无空洞/重叠")
    else:
        for i in gaps:
            _, prev_end = spans[i]
            cur_start, _ = spans[i + 1]
            kind = "空洞" if cur_start > prev_end else "重叠"
            notes.append(f"主轨 clipitem {i}->{i + 1}: {kind} prev_end={prev_end:g} != cur_start={cur_start:g}")
    if first_start != 0:
        notes.append(f"主轨首个 clipitem start={first_start:g} != 0（真实多轨产物允许；RVE 契约模式会另判）")
    return info, problems, notes


def _ntsc_flag(fps: float) -> str:
    return "TRUE" if any(abs(fps - r) <= _NTSC_TOL for r in _NTSC_RATES) else "FALSE"


def verify_against_spec(p: Parsed, spec_path: Path) -> tuple[list[str], list[str], list[str]]:
    """RVE 契约模式：把解析结果与独立加载的 spec.json 逐镜比对。"""
    info: list[str] = []
    problems: list[str] = []
    notes: list[str] = []
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    fps = float(spec["source"]["fps"])
    src_dur = float(spec["source"]["duration_sec"])
    has_audio = bool(spec["source"].get("has_audio", True))
    shots = sorted(spec.get("shots", []), key=lambda s: s["in_sec"])

    # 帧率口径
    exp_tb = int(round(fps))
    if p.seq_timebase is not None and int(round(p.seq_timebase)) != exp_tb:
        problems.append(f"序列 <timebase>={p.seq_timebase:g} != round(spec fps {fps})={exp_tb}")
    if p.seq_ntsc is not None and p.seq_ntsc != _ntsc_flag(fps):
        problems.append(f"序列 <ntsc>={p.seq_ntsc} 与 spec fps {fps} 不符（应 {_ntsc_flag(fps)}）")
    info.append(f"帧率: <timebase>={p.seq_timebase:g} <ntsc>={p.seq_ntsc}  (spec fps={fps:g})")

    track = p.primary
    if track is None:
        problems.append("未找到可作为主轨的 video track（无 clipitem）")
        return info, problems, notes
    clips = track.clips

    if len(clips) != len(shots):
        problems.append(f"video clipitem 数 {len(clips)} != EditSpec 镜头数 {len(shots)}")

    exp_start = 0
    for i, shot in enumerate(shots):
        in_f = int(round(shot["in_sec"] * fps))
        out_f = int(round(shot["out_sec"] * fps))
        dur_f = out_f - in_f
        if i >= len(clips):
            break
        c = clips[i]
        got = (c.start, c.end, c.cin, c.cout, c.duration)
        exp = (exp_start, exp_start + dur_f, in_f, out_f, dur_f)

        # 时间轴 span 自洽（end-start == out-in == <duration>）
        if c.start is not None and c.end is not None and c.cin is not None and c.cout is not None:
            if abs((c.end - c.start) - (c.cout - c.cin)) > _FRAME_TOL:
                problems.append(
                    f"镜 {i}: end-start={c.end - c.start:g} != out-in={c.cout - c.cin:g}（时间轴跨度与素材区间不自洽）"
                )
        for label, g, e in zip(("start", "end", "in", "out", "<duration>"), got, exp):
            if g is None:
                problems.append(f"镜 {i}: 缺 {label}")
            elif abs(g - e) > _FRAME_TOL:
                problems.append(f"镜 {i}: {label}={g:g} != spec 期望 {e:g}")
        exp_start += dur_f

    # sequence duration
    exp_total = int(round(src_dur * fps))
    if p.duration is None:
        problems.append("sequence 缺 <duration>")
    else:
        if abs(p.duration - exp_total) > _FRAME_TOL:
            problems.append(f"sequence duration={p.duration:g} != round(spec source.duration_sec×fps)={exp_total}")
        spans = _spans(clips)
        if spans and abs(spans[-1][1] - p.duration) > _FRAME_TOL:
            problems.append(f"末镜 end={spans[-1][1]:g} != sequence duration={p.duration:g}")
    info.append(f"sequence duration={p.duration:g}  期望={exp_total}")

    # 首镜起点
    spans = _spans(clips)
    if spans and spans[0][0] != 0:
        problems.append(f"首个 clipitem start 应为 0，实为 {spans[0][0]:g}")

    # 时间轴空洞/重叠（RVE 契约要求从 0 起连续铺满）
    if spans and spans[0][0] == 0:
        for i, ((_, prev_end), (cur_start, _)) in enumerate(zip(spans, spans[1:])):
            if abs(cur_start - prev_end) > _FRAME_TOL:
                kind = "空洞" if cur_start > prev_end else "重叠"
                problems.append(
                    f"镜 {i}->{i + 1}: 时间轴{kind} prev_end={prev_end:g} != cur_start={cur_start:g}"
                )

    # 转场：严格跨骑剪切点 + 数量与 spec 渐变镜一致
    cuts = [c.start for c in clips]
    straddle_bad = 0
    for ti, (ts, te) in enumerate(track.transitions):
        if ts is None or te is None:
            problems.append(f"transitionitem {ti}: start/end 未设，无法判定跨骑")
            straddle_bad += 1
            continue
        following = [s for s in cuts if s is not None and s > ts]
        cut = min(following) if following else None
        if cut is None:
            problems.append(f"transitionitem {ti}: 之后无 clipitem，无法确定剪切点")
            straddle_bad += 1
        elif not (ts < cut < te):
            problems.append(f"transitionitem {ti}: 未严格跨骑剪切点，[{ts:g}, {te:g}) 未包住 cut={cut:g}")
            straddle_bad += 1
    exp_trans = sum(
        1 for s in shots if s.get("cut_type") in ("dissolve", "fade") and int(s["index"]) > 0
    )
    if len(track.transitions) != exp_trans:
        problems.append(f"transitionitem 数 {len(track.transitions)} != spec 渐变镜数 {exp_trans}")
    info.append(
        f"转场: {len(track.transitions)} 个 transitionitem（期望 {exp_trans}）；"
        + ("全部严格跨骑剪切点" if straddle_bad == 0 else f"{straddle_bad} 个未跨骑剪切点")
    )

    # <timecode>
    if not p.has_timecode:
        problems.append("sequence 缺 <timecode>")
    info.append(f"<timecode>: {'存在' if p.has_timecode else '缺失'}")

    # 音频轨
    a_items = sum(len(t.clips) for t in p.audio_tracks)
    if has_audio and a_items == 0:
        problems.append(f"spec source.has_audio=True 但 XML 无音频轨 clipitem")
    if not has_audio and a_items > 0:
        problems.append(f"spec source.has_audio=False 但 XML 含 {a_items} 个音频 clipitem")
    info.append(
        f"音频轨: {'存在' if a_items else '无'}（{a_items} 个 clipitem; spec has_audio={has_audio}）"
    )
    return info, problems, notes


# --------------------------------------------------------------------------- #
# 输出与入口
# --------------------------------------------------------------------------- #
def run(xml_path: Path, spec_path: Path | None) -> tuple[bool, list[str], dict]:
    """执行校验，返回 (是否通过, 可读报告行, 机器结果)。"""
    lines: list[str] = []
    problems: list[str] = []
    notes: list[str] = []
    mode = "RVE 契约模式（含 spec 比对）" if spec_path else "结构模式（仅解析）"
    lines.append("独立校验器 independent_verify")
    lines.append("=" * 62)
    lines.append(f"XML : {xml_path}")
    lines.append(f"模式: {mode}")

    try:
        parsed = parse_xmeml(xml_path)
    except Exception as exc:  # noqa: BLE001 —— 解析失败即判失败
        lines.append(f"读取器: 解析失败 —— {type(exc).__name__}: {exc}")
        return False, lines + [f"[FAIL] 解析 XML 失败: {type(exc).__name__}: {exc}"], {"verdict": "FAIL"}

    lines.append(
        f"读取器: 根标签 <{parsed.root_tag}> version={parsed.version} "
        f"{'带 DOCTYPE' if parsed.doctype else '无 DOCTYPE'}"
    )
    lines.append(
        f"读取器: video 轨 {len(parsed.video_tracks)} 条 / audio 轨 {len(parsed.audio_tracks)} 条"
    )
    if parsed.root_tag != "xmeml":
        problems.append(f"根标签应为 'xmeml'，实为 '{parsed.root_tag}'")
    if not parsed.video_tracks:
        notes.append("该 XML 无可重建的 <video><track>（读取器已成功解析其结构）")
    elif not any(t.clips for t in parsed.video_tracks):
        notes.append("video 轨内无 <clipitem>（读取器已成功解析其结构）")

    if parsed.primary is not None:
        info, probs, nts = reconstruct_timeline(parsed.primary.clips)
        lines.append(f"主轨: {parsed.primary.index} 号轨（{len(parsed.primary.clips)} clipitem）")
        lines += [f"  {x}" for x in info]
        problems += probs
        notes += nts
    else:
        lines.append("主轨: 无")

    if spec_path is not None:
        if not Path(spec_path).exists():
            problems.append(f"spec.json 不存在: {spec_path}")
        else:
            sinfo, sprobs, snotes = verify_against_spec(parsed, spec_path)
            lines += [f"  {x}" for x in sinfo]
            problems += sprobs
            notes += snotes
    else:
        # 结构模式的观察项：真实产物中这些是合法形态，仅作报告
        for t in parsed.video_tracks:
            if t.clips:
                spans = _spans(t.clips)
                if spans and spans[0][0] != 0:
                    notes.append(f"track#{t.index} 从 {spans[0][0]:g} 开始（非 0）")

    lines.append("-" * 62)
    for n in dict.fromkeys(notes):
        lines.append(f"[note] {n}")
    if problems:
        for pr in problems:
            lines.append(f"[FAIL] {pr}")
        lines.append(f"裁决: FAIL（{len(problems)} 项不一致）")
        return False, lines, {"verdict": "FAIL", "problems": problems, "notes": list(dict.fromkeys(notes))}
    lines.append("裁决: PASS —— 时间轴重建与逐镜比对全部成立")
    return True, lines, {"verdict": "PASS", "notes": list(dict.fromkeys(notes))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="independent_verify",
        description="独立校验 FCP7 XML（xmeml）与 EditSpec 是否一致（纯标准库，不依赖 rve）。",
    )
    parser.add_argument("xml", help="待校验的 xmeml 文件")
    parser.add_argument("--spec", default=None, help="EditSpec spec.json（提供则进入 RVE 契约模式）")
    parser.add_argument("--json", action="store_true", help="末行输出机器可读 JSON")
    args = parser.parse_args(argv)

    ok, lines, result = run(Path(args.xml), Path(args.spec) if args.spec else None)
    for line in lines:
        print(line)
    if args.json:
        print("JSON: " + json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
