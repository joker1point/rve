"""生成「带设计期真值」的合成素材 + ground_truth.json —— 给模拟 NLE 导入用。

为什么需要它
------------
真人在 Premiere / Resolve 里导入时间线后，人工核对的是三件事：
镜头数、时间线总长、每段落在哪、素材有没有链接上。但本机**没有 NLE**，
真人那一步做不了。

于是换一种做法：**造一段真值在设计期就完全已知的素材**——
每一段画面的颜色、帧数、每两个段落之间是硬切/溶解/淡出，全部由 ffmpeg
命令精确决定。因此「时间线该长什么样」不依赖 RVE 的检测结果，而是**先验事实**。

这样比对就不构成循环论证：
- 真值基准 = ffmpeg 命令的帧数（素材固有属性，与 RVE 无关）
- 被测对象 = RVE 导出的 FCP7 XML 经**独立解析器**重建出的时间线

素材设计（30fps 整数帧率，帧数精确可算）
------------------------------------------
| 段 | 画面          | 帧数 | 秒    | 进入本段的转场 |
|----|---------------|------|-------|----------------|
| 0  | 纯红          | 60   | 2.0   | —              |
| 1  | SMPTE 彩条    | 45   | 1.5   | 硬切           |
| 2  | 纯绿          | 36   | 1.2   | 硬切           |
| 3  | 纯蓝          | 30   | 1.0   | 硬切           |
| 4  | 纯黄          | 30   | 1.0   | 溶解 0.5s      |
| 5  | 纯黑          | 30   | 1.0   | 淡出 0.5s      |

合成过程（帧级推导）：
    concat(0..3)  = 60+45+36+30 = 171 帧 = 5.7s
    xfade(171帧, 段4) d=15f offset=171-15=156帧(5.2s) → 186 帧 = 6.2s
    fade out st=171f(5.7s) d=15f                                  → 仍 186 帧
    concat(+段5)  = 186 + 30 = 216 帧 = 7.2s

于是时间线真值：
    镜头起点 = [0.0, 2.0, 3.5, 4.7, 5.2, 5.7]，总长 7.2s
    镜头时长 = [2.0, 1.5, 1.2, 0.5, 0.5, 1.5]
    转场     = 溶解 @5.2s、淡出 @5.7s（各 0.5s）

两个刻意的设计约束（都是踩过的坑）：
- **每段必须是静态画面**：动态画面自身的逐帧差会让转场判据把硬切误判成渐变。
- **淡出后必须留足黑场**：转场判据只取边界 ±1s 窗口，窗口盖不住整段亮度塌陷
  就会落进 dissolve 分支。段5 是 1.0s 纯黑，覆盖充分。

用法::

    python scripts/make_nle_ground_truth.py --out <目录>

产出：``<目录>/nle_fixture.mp4`` 与 ``<目录>/ground_truth.json``。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

FPS = 30
SIZE = "320x240"

# 段定义：(名称, 画面描述, 帧数)
SEGMENTS: list[tuple[str, str, int]] = [
    ("red", "纯红", 60),
    ("bars", "SMPTE 彩条", 45),
    ("green", "纯绿", 36),
    ("blue", "纯蓝", 30),
    ("yellow", "纯黄", 30),
    ("black", "纯黑", 30),
]

# 进入第 4 段是溶解、进入第 5 段是淡出；其余是硬切
DISSOLVE_AT_SEGMENT = 4
FADE_AT_SEGMENT = 5
TRANSITION_FRAMES = 15  # 0.5s @30fps

# 每段的画面生成表达式（静态画面）；{size}/{fps}/{dur} 由代码填
_PICTURES = {
    "red": "color=c=red:s={size}:r={fps}:d={dur}",
    "bars": "smptebars=s={size}:r={fps}:d={dur}",
    "green": "color=c=green:s={size}:r={fps}:d={dur}",
    "blue": "color=c=blue:s={size}:r={fps}:d={dur}",
    "yellow": "color=c=yellow:s={size}:r={fps}:d={dur}",
    "black": "color=c=black:s={size}:r={fps}:d={dur}",
}

_VIDEO_NAME = "nle_fixture.mp4"
_TRUTH_NAME = "ground_truth.json"


def _sec(frames: int) -> float:
    """帧 → 秒。"""
    return round(frames / FPS, 6)


def _build_truth() -> dict:
    """由段定义推出时间线真值（纯计算，不跑 ffmpeg）。

    真值用**内容事件**表述，而不是「镜头起点」：
    - 硬切点 = 画面内容突变的那一帧（确定事实）；
    - 转场 = 一**段时间窗**（渐变发生的区间），不是某个点。

    为什么要这样表述：渐变区的归属是约定而非事实——它可以算前一镜的一部分、
    后一镜的一部分，或单独成一镜。断言「镜头起点必须等于 X」等于把约定当成
    事实；而「硬切点必须出现在 X」「转场必须落在 X 这段时间窗里」是素材侧
    **不容争辩**的事实。

    帧级推导（与 ffmpeg 命令一一对应）：
        concat(red60, bars45, green36, blue30)  = head = 171 帧 (5.7s)
        xfade(blue, yellow, d=15, offset=head-15)  交叠区 = [156, 171) = [5.2, 5.7)
        fade=t=out:st=head:d=15                     淡出区 = [171, 186) = [5.7, 6.2)
        concat(+black30)                            纯黑区 = [186, 216)  总长 216 帧
    """
    head = sum(f for _, _, f in SEGMENTS[:DISSOLVE_AT_SEGMENT])
    dissolve_start = head - TRANSITION_FRAMES
    fade_start = head
    fade_end = head + TRANSITION_FRAMES
    total_frames = fade_end + SEGMENTS[-1][2]

    # 硬切点：除渐变区外，每一段的起点都是一次内容突变
    hard_cuts: list[int] = []
    cursor = 0
    for idx, (_, _, frames) in enumerate(SEGMENTS[:DISSOLVE_AT_SEGMENT]):
        if idx > 0:
            hard_cuts.append(cursor)
        cursor += frames

    # 素材侧各段在输出时间轴上的区间（含渐变区，如实记录）
    spans: list[dict] = []
    cursor = 0
    for idx, (name, label, frames) in enumerate(SEGMENTS):
        if idx == DISSOLVE_AT_SEGMENT:
            start, end = dissolve_start, fade_end
        elif idx == FADE_AT_SEGMENT:
            start, end = fade_end, total_frames
        else:
            start, end = cursor, cursor + frames
        spans.append(
            {
                "index": idx,
                "label": label,
                "picture": name,
                "start_sec": _sec(start),
                "end_sec": _sec(end),
            }
        )
        cursor += frames

    return {
        "source": "scripts/make_nle_ground_truth.py（设计期真值，与 RVE 的检测结果无关）",
        "fps": FPS,
        "width": 320,
        "height": 240,
        "total_duration_sec": _sec(total_frames),
        "total_frames": total_frames,
        # 纯硬切切出「硬切数 + 1」镜；两处渐变各可能额外切一刀 → 区间而非单值
        "shot_count_min": len(hard_cuts) + 1,
        "shot_count_max": len(hard_cuts) + 1 + 2,
        "hard_cuts_sec": [_sec(f) for f in hard_cuts],
        "transitions": [
            {
                "type": "dissolve",
                "span_sec": [_sec(dissolve_start), _sec(fade_start)],
                "duration_sec": _sec(TRANSITION_FRAMES),
            },
            {
                "type": "fade",
                "span_sec": [_sec(fade_start), _sec(fade_end)],
                "duration_sec": _sec(TRANSITION_FRAMES),
            },
        ],
        "segments": spans,
        "expect_audio": True,
        "tolerances": {
            "duration_sec": 0.05,
            "hard_cut_sec": 0.1,
            "transition_window_sec": 0.5,
        },
    }


def _ffmpeg_command(out_path: Path) -> list[str]:
    """构造合成命令：concat → xfade(溶解) → fade(淡出) → concat(黑场)。"""
    args: list[str] = ["ffmpeg", "-y", "-v", "error"]
    for name, _, frames in SEGMENTS:
        args += [
            "-f",
            "lavfi",
            "-i",
            _PICTURES[name].format(size=SIZE, fps=FPS, dur=frames / FPS),
        ]
    # 音频：一段正弦（让时间线带真实音轨，模拟真人会看的第二条轨）。
    # 刻意比视频**短 0.3s**：aac 会把尾部补齐到整帧，若音频与视频等长，
    # 容器 format.duration 会比视频实际帧长多出 ~22ms，而 RVE 的探针优先取
    # 容器 duration（providers/ffprobe.py 的 _parse_duration），真值就对不齐。
    # 音频短于视频时，容器时长由视频流决定 = 216 帧 / 30fps = 7.2s。
    total_sec = _build_truth()["total_duration_sec"]
    audio_sec = max(0.5, total_sec - 0.3)
    args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={audio_sec}"]

    head = sum(f for _, _, f in SEGMENTS[:DISSOLVE_AT_SEGMENT])
    offset = _sec(head - TRANSITION_FRAMES)
    n = len(SEGMENTS)
    # 每个输入先统一 timebase 到 1/30：concat / xfade 都要求各输入 timebase 一致，
    # 而 lavfi 源的 timebase 各不相同（color 是 1/1000000，smptebars 是 1/30）；
    # concat 的输出 timebase 会被重置成 AVTB(1/1000000)，故每一步输出都要再 settb。
    norm = "".join(f"[{i}:v]settb=1/30[v{i}];" for i in range(n))
    chain = (
        f"{norm}"
        f"[v0][v1][v2][v3]concat=n=4:v=1:a=0,settb=1/30[head];"
        f"[head][v4]xfade=transition=fade:duration={_sec(TRANSITION_FRAMES)}:offset={offset},"
        f"settb=1/30[diss];"
        f"[diss]fade=t=out:st={_sec(head)}:d={_sec(TRANSITION_FRAMES)}[faded];"
        f"[faded]settb=1/30[faded2];"
        f"[faded2][v5]concat=n=2:v=1:a=0,settb=1/30[v]"
    )
    args += [
        "-filter_complex",
        chain,
        "-map",
        "[v]",
        "-map",
        f"{n}:a",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-r",
        str(FPS),
        "-c:a",
        "aac",
        # 截断输出到设计时长：aac 编码会补齐到整帧，令容器 format.duration 比
        # 视频实际帧长多出几毫秒；而 RVE 的探针优先取容器 duration
        # （providers/ffprobe.py 的 _parse_duration），不截断则真值对不齐。
        "-t",
        str(total_sec),
        str(out_path),
    ]
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成带设计期真值的合成素材 + ground_truth.json"
    )
    parser.add_argument("--out", required=True, help="输出目录（写入视频与真值 JSON）")
    args = parser.parse_args(argv)

    if shutil.which("ffmpeg") is None:
        print("错误：未找到 ffmpeg，无法生成合成素材。", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    video = out_dir / _VIDEO_NAME
    truth = _build_truth()

    cmd = _ffmpeg_command(video)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(f"错误：ffmpeg 合成失败（退出码 {proc.returncode}）", file=sys.stderr)
        print((proc.stderr or "").strip()[-800:], file=sys.stderr)
        print("命令：" + " ".join(cmd), file=sys.stderr)
        return 1
    if not video.is_file():
        print(f"错误：ffmpeg 未产出 {video}", file=sys.stderr)
        return 1

    truth_path = out_dir / _TRUTH_NAME
    truth_path.write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"素材: {video}")
    print(f"真值: {truth_path}")
    print(
        f"设计期真值: {truth['shot_count_min']}~{truth['shot_count_max']} 镜 / "
        f"{truth['total_duration_sec']}s / 硬切 {len(truth['hard_cuts_sec'])} 处 / "
        f"转场 {len(truth['transitions'])} 处"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
