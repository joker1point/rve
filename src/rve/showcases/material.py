"""案例素材准备：程序合成（synthetic）与外部素材（external）。

红线：
- **绝不联网自动下载**：外部素材缺失时显式报"素材缺失 + 怎么准备"，
  既避免 CI 不稳定，也避免版权风险。
- ffmpeg 一律走**子进程**调用本机已安装的可执行文件，不 vendor 上游源码。
- 合成素材的 `prepare` 是**可执行**的命令行（每条以 ffmpeg 开头），
  其中的 `{{out}}` 会被替换为输出文件路径；外部素材的 `prepare` 只是给人看的说明。
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

from .models import ShowcaseCase

# 合成命令里的输出文件占位符
OUT_TOKEN = "{{out}}"

_FFMPEG_NAMES = ("ffmpeg", "ffmpeg.exe")


class MaterialMissing(RuntimeError):
    """素材缺失 / 无法准备。

    消息里必须写清"怎么准备素材"：素材名称、许可、来源、准备命令、放置位置。
    """


def _ffmpeg_exe() -> str:
    """定位本机 ffmpeg；找不到时抛带人话的 MaterialMissing。"""
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise MaterialMissing(
            "未找到 ffmpeg 可执行文件，无法准备合成素材。\n"
            "怎么准备：安装 ffmpeg（含 ffprobe）并加入 PATH，"
            "下载地址 https://ffmpeg.org/download.html ；"
            "装好后重跑本案例即可。"
        )
    return exe


def _split_command(command: str) -> list[str]:
    """把一条命令行拆成 argv。

    合成命令按 POSIX 规则拆分（引号/转义由 shlex 处理），因此命令里不要写
    反斜杠路径与 shell 元字符；输出路径由代码在拆分**之后**注入，不受此限制。
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise MaterialMissing(f"素材准备命令无法解析（{command}）：{exc}") from exc
    if not argv:
        raise MaterialMissing(f"素材准备命令为空：{command!r}")
    if Path(argv[0]).name.lower() not in _FFMPEG_NAMES:
        raise MaterialMissing(
            f"素材准备命令必须以 ffmpeg 开头，实际为：{command!r}"
        )
    return argv


def _generate_synthetic(case: ShowcaseCase, workdir: Path) -> Path:
    """按 prepare 里的命令在 workdir 内合成素材，返回产物路径。

    约定：产物名为 `<case_id>.mp4`（与外部素材的查找路径一致）。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / f"{case.id}.mp4"
    commands = case.material.prepare
    if not commands:
        raise MaterialMissing(
            f"案例 {case.id} 的素材配方缺少 prepare 命令，无法合成素材。\n"
            "怎么准备：在 case.json 的 material.prepare 里补上 ffmpeg 合成命令。"
        )

    exe = _ffmpeg_exe()
    for command in commands:
        argv = _split_command(command)
        argv[0] = exe
        if OUT_TOKEN in argv:
            argv = [target.as_posix() if part == OUT_TOKEN else part for part in argv]
        proc = subprocess.run(
            argv,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()[-600:]
            raise MaterialMissing(
                f"合成素材失败（ffmpeg 退出码 {proc.returncode}）。\n"
                f"命令：{command}\n"
                f"怎么准备：用这条命令手工跑一遍看报错：{command.replace(OUT_TOKEN, target.as_posix())}\n"
                f"ffmpeg 输出：{detail}"
            )

    if not target.is_file():
        raise MaterialMissing(
            f"素材合成命令执行完毕但没有产出 {target}。\n"
            f"怎么准备：确认 prepare 命令把输出写到了 {target.name}"
            f"（可用 {OUT_TOKEN} 占位符）。"
        )
    return target


def _external_hint(case: ShowcaseCase, target: Path) -> str:
    """拼装外部素材缺失时的指引：名称、许可、来源、准备命令、放置位置。"""
    material = case.material
    lines = [
        f"案例 {case.id} 缺少外部素材：{case.title}",
        f"素材来源：{material.source}",
        f"许可：{material.license}",
    ]
    if material.prepare:
        lines.append("怎么准备（按顺序执行）：")
        lines.extend(f"  {step}" for step in material.prepare)
    if material.local_hint:
        lines.append(f"放置指引：{material.local_hint}")
    lines.append(
        f"怎么准备：把素材放到 {target}，或用 --material <本地文件路径> 指定已有文件。"
    )
    return "\n".join(lines)


def _resolve_external(
    case: ShowcaseCase, workdir: Path, material_override: str | None
) -> Path:
    """定位外部素材：优先 --material，其次工作目录下的 <case_id>.mp4。"""
    if material_override:
        path = Path(material_override).expanduser()
        if path.is_file():
            return path
        target = workdir / f"{case.id}.mp4"
        raise MaterialMissing(
            f"通过 --material 指定的素材不存在：{path}\n" + _external_hint(case, target)
        )
    target = workdir / f"{case.id}.mp4"
    if target.is_file():
        return target
    raise MaterialMissing(_external_hint(case, target))


def ensure_material(
    case: ShowcaseCase,
    workdir: Path,
    material_override: str | None = None,
) -> Path:
    """确保案例素材就绪，返回可用的本地文件路径。

    synthetic：按 material.prepare 用 ffmpeg 现场合成到 workdir。
    external ：优先用 material_override 指向的本地文件，其次 workdir/<case_id>.mp4；
                两者都没有时抛 MaterialMissing（消息里写清怎么准备）。

    绝不联网下载。
    """
    workdir = Path(workdir)
    kind = case.material.kind
    if kind == "synthetic":
        return _generate_synthetic(case, workdir)
    if kind == "external":
        return _resolve_external(case, workdir, material_override)
    raise MaterialMissing(
        f"未知的素材类型 {kind!r}（仅支持 synthetic / external），"
        f"案例 {case.id} 无法准备素材。"
    )
