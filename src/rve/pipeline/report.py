"""分镜报告渲染：把 EditSpec 输出成人类可读的 HTML 分镜报告。

设计 directive：**简洁、可打印、自包含单文件**（无外部 CSS/JS/字体依赖，
双击即可在任意浏览器打开，Ctrl+P 可直接出纸质稿）。

与 export.py 一样，读取 spec 走 `rve.workspace.read_spec`（延迟导入）。
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from rve.schema.editspec import EditSpec
from rve.schema.results import ExportResult, ToolReceipt


def _default_out_path(spec_id: str) -> Path:
    """默认报告路径：cwd/reports/<spec_id>_report.html。"""
    return Path.cwd() / "reports" / f"{spec_id}_report.html"


def _e(value: object) -> str:
    """HTML 转义（None → 空串）。"""
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_sec(value: float) -> str:
    """秒保留三位小数，便于人工核对时间锚点。"""
    return f"{value:.3f}"


# 自包含、可打印的极简样式
_CSS = """
  :root { --fg:#1b1f24; --muted:#5b6572; --line:#d7dce3; --accent:#2f6fed; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         color: var(--fg); margin: 0 auto; max-width: 960px; padding: 24px;
         line-height: 1.5; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  h2 { font-size: 15px; margin: 28px 0 8px; padding-bottom: 4px;
       border-bottom: 2px solid var(--line); }
  .meta { color: var(--muted); font-size: 13px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { border: 1px solid var(--line); padding: 5px 8px; text-align: left;
           vertical-align: top; }
  th { background: #f3f5f8; font-weight: 600; white-space: nowrap; }
  td.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .pill { display: inline-block; padding: 1px 7px; border-radius: 10px;
          background: #eef2fb; color: var(--accent); font-size: 12px; }
  .warn { border-left: 3px solid #d9822b; background: #fff7ef; padding: 6px 10px;
          font-size: 13px; margin: 4px 0; }
  .empty { color: var(--muted); font-size: 13px; }
  @media print { body { max-width: none; padding: 0; }
                 h2 { break-after: avoid; } tr { break-inside: avoid; } }
"""


def _source_section(spec: EditSpec) -> str:
    src = spec.source
    rows = [
        ("源文件", src.path),
        ("时长/帧率", f"{_fmt_sec(src.duration_sec)} s @ {src.fps} fps"),
        ("分辨率", f"{src.width}×{src.height}"),
        ("含音频", "是" if src.has_audio else "否"),
    ]
    body = "".join(
        f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows
    )
    return f"<h2>源素材</h2><table>{body}</table>"


def _rhythm_section(spec: EditSpec) -> str:
    r = spec.rhythm
    rows = [
        ("镜头数", r.shot_count or len(spec.shots)),
        ("平均镜长", f"{_fmt_sec(r.avg_shot_sec)} s"),
        ("中位镜长", f"{_fmt_sec(r.median_shot_sec)} s"),
        ("最短/最长", f"{_fmt_sec(r.shortest_shot_sec)} / {_fmt_sec(r.longest_shot_sec)} s"),
        ("每分钟切点", _fmt_sec(r.cuts_per_min)),
        ("节奏曲线", r.cut_curve),
    ]
    body = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows)
    return f"<h2>节奏统计</h2><table>{body}</table>"


def _shots_section(spec: EditSpec) -> str:
    head = (
        "<tr><th>#</th><th>入点(s)</th><th>出点(s)</th><th>时长(s)</th>"
        "<th>切点</th><th>景别</th><th>运镜</th><th>置信</th><th>描述</th></tr>"
    )
    rows = []
    for s in spec.shots:
        cut = (
            f'<span class="pill">{_e(s.cut_type)}</span>'
            if s.transition is not None
            else _e(s.cut_type)
        )
        rows.append(
            "<tr>"
            f'<td class="num">{s.index}</td>'
            f'<td class="num">{_fmt_sec(s.in_sec)}</td>'
            f'<td class="num">{_fmt_sec(s.out_sec)}</td>'
            f'<td class="num">{_fmt_sec(s.duration_sec)}</td>'
            f"<td>{cut}</td>"
            f"<td>{_e(s.shot_size)}</td>"
            f"<td>{_e(s.camera_move)}</td>"
            f'<td class="num">{s.confidence.overall:.2f}</td>'
            f"<td>{_e(s.description)}</td>"
            "</tr>"
        )
    return f"<h2>镜头表</h2><table>{head}{''.join(rows)}</table>"


def _transitions_section(spec: EditSpec) -> str:
    items = [s for s in spec.shots if s.transition is not None]
    if not items:
        return '<h2>转场</h2><p class="empty">无渐变转场（全硬切）。</p>'
    head = "<tr><th>#</th><th>类型</th><th>时长(s)</th><th>置信</th></tr>"
    rows = "".join(
        "<tr>"
        f'<td class="num">{s.index}</td>'
        f"<td>{_e(s.transition.type)}</td>"
        f'<td class="num">{_fmt_sec(s.transition.duration_sec)}</td>'
        f'<td class="num">{s.transition.confidence:.2f}</td>'
        "</tr>"
        for s in items
    )
    return f"<h2>转场</h2><table>{head}{rows}</table>"


def _subtitles_section(spec: EditSpec) -> str:
    if not spec.subtitles:
        return ""
    head = "<tr><th>入点(s)</th><th>出点(s)</th><th>来源</th><th>文本</th></tr>"
    rows = "".join(
        "<tr>"
        f'<td class="num">{_fmt_sec(t.in_sec)}</td>'
        f'<td class="num">{_fmt_sec(t.out_sec)}</td>'
        f"<td>{_e(t.source)}</td>"
        f"<td>{_e(t.text)}</td>"
        "</tr>"
        for t in spec.subtitles
    )
    return f"<h2>字幕</h2><table>{head}{rows}</table>"


def _uncertain_section(spec: EditSpec) -> str:
    blocks = []
    for u in spec.uncertain:
        cand = f" 候选：{_e('、'.join(u.candidates))}" if u.candidates else ""
        blocks.append(
            f'<div class="warn"><b>{_e(u.field_path)}</b> — {_e(u.reason)}{cand}</div>'
        )
    for w in spec.warnings:
        blocks.append(f'<div class="warn">{_e(w)}</div>')
    if not blocks:
        return '<h2>存疑与告警</h2><p class="empty">无。</p>'
    return f"<h2>存疑与告警</h2>{''.join(blocks)}"


def _build_html(spec: EditSpec) -> str:
    """渲染整份报告为自包含 HTML 字符串。"""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>RVE 分镜报告 — {_e(spec.spec_id)}</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n"
        f"<h1>RVE 分镜报告</h1>\n"
        f'<p class="meta">spec_id: {_e(spec.spec_id)} · schema {_e(spec.schema_version)}'
        f" · 生成于 {_e(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</p>\n"
        f"{_source_section(spec)}\n"
        f"{_rhythm_section(spec)}\n"
        f"{_shots_section(spec)}\n"
        f"{_transitions_section(spec)}\n"
        f"{_subtitles_section(spec)}\n"
        f"{_uncertain_section(spec)}\n"
        "</body>\n</html>\n"
    )


async def render_report(spec_id: str, report_format: str = "html") -> ExportResult:
    """生成人类可读分镜报告（当前仅支持 html）。"""
    from rve.workspace import read_spec  # 延迟导入：workspace 由并行模块提供

    started = datetime.now()
    if report_format != "html":
        raise NotImplementedError(f"暂不支持的报告格式: {report_format}")

    spec = read_spec(spec_id)
    path = _default_out_path(spec_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_build_html(spec), encoding="utf-8")

    duration_ms = int((datetime.now() - started).total_seconds() * 1000)
    receipt = ToolReceipt(
        tool="render_report",
        params={"spec_id": spec_id, "report_format": report_format},
        ok=True,
        started_at=started,
        duration_ms=duration_ms,
        artifacts=[str(path)],
    )
    return ExportResult(format=report_format, out_path=str(path), receipt=receipt)
