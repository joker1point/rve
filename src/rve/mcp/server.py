"""RVE MCP 工具面 —— 对 AI Agent 暴露的 8 个能力。

设计约束：
- 每个工具都是 async 薄封装：只做参数转发，不夹带业务逻辑。
- `mcp` 包与 pipeline/workspace 兄弟模块全部延迟导入：本模块可被安全 import，
  即使 `mcp` 未安装、或 pipeline 尚未就位，也只是"构建/调用"时才失败。
- docstring 是给 AI Agent 读的接口说明，务必写清「做什么 / 参数 / 失败时会怎样」。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rve.schema.editspec import EditSpec
from rve.schema.enums import DetectEngine, ExportFormat, VlmProvider
from rve.schema.results import (
    AudioAnalysisResult,
    BuildSpecResult,
    ExportResult,
    ShotDescriptionResult,
    ShotDetectionResult,
    VideoProbeResult,
)

if TYPE_CHECKING:  # 仅供类型检查，运行期不导入 mcp
    try:  # mcp >= 2：FastMCP 已更名为 MCPServer
        from mcp.server.mcpserver import MCPServer
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as MCPServer


# --------------------------------------------------------------------------- #
# 工具实现（薄封装）
# --------------------------------------------------------------------------- #


async def probe_video(path: str) -> VideoProbeResult:
    """探测单个视频文件的源信息（时长 / 帧率 / 分辨率 / 音轨等）。

    做什么:
        经由 ffprobe 读取容器与流元数据，产出 VideoSource 与可复核 receipt。
        这是整条流水线的起点——后续所有时间锚定都以这里的 duration/fps 为准。

    参数:
        path: 本地视频文件路径（须真实存在）。

    失败时会怎样:
        - 文件不存在或无法读取 -> 抛 FileNotFoundError/运行错误，绝不静默返回空壳。
        - 本机缺少 ffprobe -> 抛运行错误。
    """
    from rve.pipeline.probe import probe_video as _probe_video

    return await _probe_video(path)


async def detect_shots(
    path: str,
    engine: DetectEngine = DetectEngine.AUTO,
    threshold: float | None = None,
    start_sec: float = 0.0,
    end_sec: float | None = None,
) -> ShotDetectionResult:
    """检测镜头边界，产出镜头列表与双引擎分歧。

    做什么:
        返回镜头切点、每个镜头的 CutType 初判，以及多引擎之间的分歧。
        双引擎结论一致处采信；分歧处不会静默取其一，而是显式写入 disagreements。

    参数:
        path: 视频文件路径。
        engine: 切镜引擎。auto=按内容自适应选择；content=纯内容检测；adaptive=自适应阈值。
        threshold: 边界判定阈值；None 表示交由引擎自定。
        start_sec: 分析起始时间（秒）。
        end_sec: 分析结束时间（秒）；None 表示分析到视频末尾。

    失败时会怎样:
        - 引擎不可用或参数非法（如 start_sec 为负）-> 抛异常。
        - 分歧不会被吞掉：会进入 disagreements，等待人工或下游复核。
    """
    from rve.pipeline.detect import detect_shots as _detect_shots

    return await _detect_shots(path, engine, threshold, start_sec, end_sec)


async def analyze_audio(
    path: str,
    separate_stems: bool = True,
    diarize: bool = True,
    transcribe: bool = True,
    language: str | None = None,
    device: str = "auto",
) -> AudioAnalysisResult:
    """分离音轨、做说话人分离与语音转写。

    做什么:
        从视频（或音频）里拆出人声/BGM/音效，识别说话人分段，产出转写文本与节拍。

    参数:
        path: 视频或音频文件路径。
        separate_stems: 是否分离人声/BGM/音效。
        diarize: 是否做说话人分离。
        transcribe: 是否做语音转写。
        language: 转写语言提示；None=自动判断。
        device: 推理设备，取值 "auto"/"cpu"/"cuda"。

    Phase 0 状态:
        该能力尚未实现（Phase 2 才落地），调用会抛 NotImplementedError。
        请勿在 Phase 0 依赖本工具；build_edit_spec 可先传空音频实例。
    """
    from rve.pipeline.audio import analyze_audio as _analyze_audio

    return await _analyze_audio(
        path,
        separate_stems=separate_stems,
        diarize=diarize,
        transcribe=transcribe,
        language=language,
        device=device,
    )


async def describe_shots(
    spec_id: str,
    provider: VlmProvider = VlmProvider.OFF,
    sample: str = "diff",
    ocr: bool = True,
) -> ShotDescriptionResult:
    """为每个镜头生成画面描述与 OCR 文本。

    做什么:
        抽取每个镜头的代表帧，交给 VLM 生成一句话描述，并可同时做画面文字 OCR。

    参数:
        spec_id: 目标 EditSpec 的 id。
        provider: VLM 来源。off=不调用任何模型；local=本地模型；cloud=云端模型。
        sample: 抽帧策略，"diff"=按帧差挑代表帧。
        ocr: 是否提取画面文字。

    Phase 0 状态:
        该能力尚未实现（Phase 2 才落地），调用会抛 NotImplementedError。
    """
    from rve.pipeline.describe import describe_shots as _describe_shots

    return await _describe_shots(spec_id, provider=provider, sample=sample, ocr=ocr)


async def build_edit_spec(
    probe: VideoProbeResult,
    shots: ShotDetectionResult,
    audio: AudioAnalysisResult,
    descriptions: ShotDescriptionResult | None = None,
    spec_id: str | None = None,
) -> BuildSpecResult:
    """把探测 / 切镜 / 音频结果组装为唯一权威结构 EditSpec。

    做什么:
        以秒为唯一时间单位，按"镜头连续、无重叠、铺满整条时间轴"的硬不变量组装
        EditSpec，并落盘到工作区。这是整个产品的中心产物。

    参数:
        probe: probe_video 的返回对象。
        shots: detect_shots 的返回对象。
        audio: analyze_audio 的返回对象；Phase 0 可传空实例（无音频信息）。
        descriptions: describe_shots 的返回（可选）。
        spec_id: 指定 spec id；None 时由实现自动生成。

    失败时会怎样:
        - 若镜头未连续铺满时间轴，EditSpec 的校验会抛 ValueError（拒绝产出半成品）。
        - 存疑字段不会用默认值蒙混，会显式登记进 uncertain[]。
    """
    from rve.pipeline.build import build_edit_spec as _build_edit_spec

    return await _build_edit_spec(
        probe, shots, audio, descriptions=descriptions, spec_id=spec_id
    )


async def get_edit_spec(spec_id: str) -> EditSpec:
    """按 id 读取已落盘的 EditSpec（权威结构本体）。

    做什么:
        返回 EditSpec 对象，供 Agent 检查镜头列表、节奏统计、存疑项与证据链。

    参数:
        spec_id: 目标 spec id。

    失败时会怎样:
        spec_id 不存在或工作区无该文件 -> 抛异常，不会返回空壳对象。
    """
    from rve.pipeline.build import get_edit_spec as _get_edit_spec

    return await _get_edit_spec(spec_id)


async def edit_spec_resource(spec_id: str) -> str:
    """editspec://{spec_id} 资源：返回该 spec 的 JSON 字符串。

    供 Agent 以资源方式直接读取 spec 全文（无需走工具调用）。
    """
    from rve.pipeline.build import get_edit_spec as _get_edit_spec

    spec = await _get_edit_spec(spec_id)
    return spec.model_dump_json()


async def export_timeline(
    spec_id: str,
    export_format: ExportFormat = ExportFormat.FCP7XML,
    out_path: str | None = None,
    fps_override: float | None = None,
) -> ExportResult:
    """把 EditSpec 导出为 NLE 可导入的时间线文件。

    做什么:
        在 exporters 内完成"秒 -> 时间码"的唯一换算，产出可被非线性剪辑软件导入
        的时间线（FCP7 XML / EDL / OTIO 等）。

    参数:
        spec_id: 目标 spec id。
        export_format: 导出格式。仅三种已实现：fcp7xml=FCP7 XML；edl=CMX EDL；
            otio=OpenTimelineIO（需装 opentimelineio）。fcpxml/resolve_xml 虽在
            ExportFormat 枚举中，但调用会抛 NotImplementedError，绝不静默产出半成品。
        out_path: 输出文件路径；None=由实现决定默认落盘位置。
        fps_override: 覆盖时间线帧率（仅影响时间码换算）；None=沿用源帧率。

    验收口径（硬性）:
        产物必须能被 Premiere/Resolve 等 NLE 真正导入并还原时间线。
        仅"生成了文件"不算通过——导入失败即视为未完成。

    失败时会怎样:
        spec_id 不存在、格式不支持或导出器缺失 -> 抛异常，绝不输出半成品文件。
    """
    from rve.pipeline.export import export_timeline as _export_timeline

    return await _export_timeline(spec_id, export_format, out_path, fps_override)


async def render_report(spec_id: str, report_format: str = "html") -> ExportResult:
    """渲染人类可读的复核报告（节奏统计 / 存疑项 / 证据链）。

    做什么:
        产出报告文件，供人工复核"是否还原了原片节奏"，并核对存疑结论。

    参数:
        spec_id: 目标 spec id。
        report_format: 报告格式，"html" 或 "json"。

    失败时会怎样:
        spec_id 不存在或格式不支持 -> 抛异常。
    """
    from rve.pipeline.report import render_report as _render_report

    return await _render_report(spec_id, report_format)


# --------------------------------------------------------------------------- #
# 服务器构建（延迟导入 mcp）
# --------------------------------------------------------------------------- #

_WORKFLOW_HINT = (
    "RVE：从一条成片逆向还原出可导入 NLE 的编辑时间线。"
    "推荐调用顺序：probe_video -> detect_shots -> "
    "(analyze_audio / describe_shots，Phase 0 未实现) -> build_edit_spec -> "
    "get_edit_spec -> export_timeline / render_report。"
)

_TOOLS = (
    probe_video,
    detect_shots,
    analyze_audio,
    describe_shots,
    build_edit_spec,
    get_edit_spec,
    export_timeline,
    render_report,
)


def build_server() -> "MCPServer":
    """构建并返回 MCP server 实例：注册全部 8 个工具 + editspec 资源。

    延迟导入说明:
        只有调用本函数时才 import `mcp`。因此"未安装 mcp"只会让构建失败，
        而 import 本模块（以及 import rve.cli）不会受牵连。

    版本适配说明:
        mcp 2.x 把 `FastMCP` 更名为 `MCPServer`（模块 `mcp.server.mcpserver`），
        而 1.x 的旧路径 `mcp.server.fastmcp` 在 2.x 里会直接抛
        ModuleNotFoundError。两个大版本对外 API 完全一致
        （`__init__(name, instructions)` / `add_tool` / `resource` / `run`），
        故按可用性择一导入 —— 免得"开发机装 1.x 绿、用户全新装 2.x 红"。
    """
    try:  # mcp >= 2
        from mcp.server.mcpserver import MCPServer
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as MCPServer

    server = MCPServer(name="rve", instructions=_WORKFLOW_HINT)
    for fn in _TOOLS:
        server.add_tool(fn)
    server.resource("editspec://{spec_id}")(edit_spec_resource)
    return server


def run_stdio() -> None:
    """以 stdio 传输启动 MCP server，供常驻 Agent 调用。"""
    build_server().run()


if __name__ == "__main__":
    run_stdio()
