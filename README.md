# RVE — Reverse Video Engineering

> 从一条**成片**逆向还原出可导入 NLE 的**编辑时间线**：`成片 → EditSpec → FCP7 XML / OTIO`。
> 以本地 **MCP Server + 同名 CLI（`rve`）** 交付，可被任意支持 MCP 的 AI Agent 编排，也能手跑。

本文档中的每条能力都对应一条**可复现的命令**，命令与真实代码路径一一对应（详见各节）。

---

## 为什么存在

现有工具覆盖不到「成片 → 可编辑时间线」这一格：

- **只能还原硬切**：多数镜头切分工具止步于"切点列表"，不产出可被剪辑软件吃下的工程；
- **绑死单一 NLE**：能导出时间线的少数方案往往只对接某一家剪辑软件，导出层不可替换；
- **封闭**：反推提示词/分镜的 SaaS 是闭源服务，无法自托管、无法被 Agent 直接调用。

RVE 的定位是 **C 型（工程还原）**：把成熟的上游工具（FFmpeg/ffprobe、PySceneDetect）**薄适配器**封装成统一流水线，产出**秒锚定**的唯一权威结构 `EditSpec`，再从它导出可导入的时间线。

---

## 安装

前置条件：本机需装 **ffmpeg（含 ffprobe）**，并加入 `PATH`。

```bash
# 尚未发布到 PyPI，先从源码安装（含开发依赖）
git clone https://github.com/joker1point/rve.git && cd rve
python -m pip install -e ".[dev]"
```

安装后即得到 `rve` 命令（`pyproject.toml` 的 `[project.scripts]`）。

自检本机环境（缺依赖时只报告、不报错）：

```bash
rve doctor
```

`doctor` 会逐项探测外部命令（`ffprobe`/`ffmpeg`）与 Python 依赖（`scenedetect`/`opentimelineio`/`mcp`），并打印缺失项的可执行安装建议。

---

## 快速上手

```bash
# 1) 一条龙：探测 -> 硬切检测 -> 组装 EditSpec -> 导出 FCP7 XML
#    产物落在 out/<spec_id>.fcp7xml.xml，同时 spec 已存入工作区
rve analyze path/to/final_cut.mp4 --out out/

# 2) 列出工作区中已保存的 spec
rve specs

# 3) 把某个 spec 导出为 NLE 时间线
rve export <spec_id> --format fcp7xml
#    --format 取值：{fcp7xml, edl, otio}（三种均已实现，见下方「导出格式」）

# 4) 渲染人类可读的分镜复核报告（HTML，自包含单文件）
rve report <spec_id>
```

一条命令即可确认上面每一步都真实可用：

| 能力 | 验证命令 | 现状 |
|---|---|---|
| 环境自检 | `rve doctor` | 可用 |
| 成片 → spec + FCP7 XML | `rve analyze <video> --out out/` | 可用（Phase 0） |
| 列出工作区 spec | `rve specs` | 可用 |
| 导出时间线 | `rve export <spec_id> --format {fcp7xml,edl,otio}` | 三种均已实现（Phase 0） |
| 分镜复核报告 | `rve report <spec_id>` | 可用（`--format html`，默认） |

`analyze` 的 `--engine` 可选 `auto`（默认，双引擎交叉验证）/`content`/`adaptive`；未实现的引擎取值会以退出码 2 明确报错，不会静默成功。

---

## 接入 AI Agent（MCP Server）

RVE 对外暴露 8 个 MCP 工具（`probe_video`、`detect_shots`、`analyze_audio`、`describe_shots`、`build_edit_spec`、`get_edit_spec`、`export_timeline`、`render_report`）与 1 个资源模板 `editspec://{spec_id}`。

在 Claude Desktop / Cursor 等的 MCP 配置中加入：

```json
{
  "mcpServers": {
    "rve": {
      "command": "python",
      "args": ["-m", "rve.mcp.server"],
      "env": {
        "RVE_WORKSPACES": "C:/Users/<you>/.rve/workspaces"
      }
    }
  }
}
```

- `command`/`args`：以 stdio 传输启动 MCP server（等价于 `rve.mcp.server:run_stdio`）。
- `env.RVE_WORKSPACES`：spec 落盘根目录；缺省为 `~/.rve/workspaces`。CLI 与 MCP 共用同一工作区，所以 Agent 产出的 spec 可以被 `rve export`/`rve report` 直接用。

---

## EditSpec 契约（唯一权威结构）

`EditSpec` 是整条流水线的中心产物，契约由 `src/rve/schema/editspec.py` 强制：

- **纯时间锚定**：所有时间字段都是秒（`in_sec`/`out_sec`/`duration_sec`…），**不存时间码**。秒↔时间码的唯一换算发生在 `src/rve/exporters/` 内，形成单一事实来源。
- **`extra="forbid"`**：所有结构模型继承自 `Strict`，未知字段一律拒绝 —— 防止 schema 静默漂移（这也是并发协作时的接口防漂移锁）。
- **镜头铺满不变量**：`shots` 必须**连续、无重叠、铺满整条时间轴**，否则 `EditSpec` 校验失败、拒绝产出（`_check_tiling` 闸门）。这条不变量正是"导出到 NLE 后出现空洞或重叠"的根因拦截点。
- **存疑显式化**：拿不准的结论进 `uncertain[]`，不用默认值蒙混。

---

## 架构

RVE 照 REA 的分层组织，三层各司其职：

| 层 | 职责 | 代码位置 |
|---|---|---|
| **工作区 / 证据目录** | 每个 spec 一个目录，内 `spec.json` 与 `receipts/`；根目录由 `RVE_WORKSPACES` 指定 | `src/rve/workspace.py` |
| **会话路由** | 把「探测 → 切镜 → 组装 → 导出」的路由暴露成 8 个 MCP 工具与 5 个 CLI 子命令 | `src/rve/mcp/server.py`、`src/rve/cli.py` |
| **Provider 注册表** | 可替换后端：探针（`FfprobeProvider`）与切镜（`PySceneDetectShotDetector`），以 `Protocol` 声明契约 | `src/rve/providers/`（`base.py` 定义 `ProbeProvider`/`ShotDetector`） |

**不 vendor 任何上游源码**：RVE **不包含**、也不复制 FFmpeg、PySceneDetect、OpenTimelineIO 的任何源码。上游工具一律以**薄适配器**封装：

- `ffprobe` 通过**子进程**调用用户机器上已安装的 `ffprobe`（`src/rve/providers/ffprobe.py`）；
- 切镜通过 **`import scenedetect`** 调用已安装的 PySceneDetect（`src/rve/providers/shots_pyscenedetect.py`）；
- OTIO 导出**延迟导入** `opentimelineio`，因此未装 OTIO 也不会拖垮其余功能。

---

## 导出格式

| 格式 | `--format` | 状态 | 说明 |
|---|---|---|---|
| FCP7 XML | `fcp7xml` | ✅ Phase 0 | 纯标准库实现（`xmeml`），产物 `<spec_id>.fcp7xml`，是 v1 主出口 |
| OTIO | `otio` | ✅ Phase 0 | 需装 `opentimelineio`；作为中间层，便于后续扩 FCPXML/Resolve |
| EDL | `edl` | ✅ Phase 0 | 纯标准库实现（CMX3600）；逐镜一条 V 轨事件，录机端时间码在序列轴上累积，转场用 `D`+帧数 |
| FCPXML | `fcpxml` | ⛔ Phase 2 | 同上（通过 MCP 的 `export_timeline` 传入） |
| Resolve XML | `resolve_xml` | ⛔ Phase 2 | 同上 |

未实现的格式**显式报错而非产出半成品**——这是 RVE 的硬规矩：绝不 quietly succeed。

---

## 诚实的局限（Phase 0）

- **只处理硬切**：转场（溶解/擦除/缩放/闪白）、`speed_ramp` 变速、复杂滤镜**尚未还原**，`analyze_audio`、`describe_shots`（逐镜描述 + OCR）在 Phase 0 未实现，字段会保持默认或进入 `uncertain[]`。
- **NLE 导入必须人工验证**：RVE 的验收口径是"产物能被 Premiere/Resolve 等**真正导入并还原时间线**"，而"生成了文件"不算通过。当前 `tests/data/sample.fcp7xml` 仍需**真人用 Premiere/Resolve 导入实测**（这是唯一未闭环的验收项）。
- **音频、字幕属 Phase 2**：音源分离、说话人分离、语音转写、字幕生成均未落地。
- **EDL 撑不起工程还原**：EDL 会丢转场/变速，仅作最小链路验证，不作为主出口。

---

## 路线图

- **Phase 0（当前）**：ffprobe 探测 + 硬切检测（content/adaptive 双引擎）+ EditSpec 组装 + FCP7 XML/OTIO 导出 + HTML 复核报告。
- **Phase 1**：完成真人 NLE 导入实测闭环；补齐 FCPXML/Resolve XML 导出；"一条命令安装"体验。
- **Phase 2**：转场识别、变速还原、音频逆向（音源分离/说话人/ASR 字幕）、逐镜描述与 OCR、TransNetV2 切镜引擎。

---

## 许可证

[MIT](LICENSE)。

---

## English

**RVE (Reverse Video Engineering)** reverses a finished video into an NLE-importable **edit timeline**: `final render → EditSpec → FCP7 XML / OTIO`. It ships as a local **MCP server** plus a CLI (`rve`), so agents (Claude, Cursor, …) and humans share the same pipeline. Phase 0 handles hard cuts only; transitions, speed ramps, audio and subtitles are Phase 2. It vendors **no** upstream source — FFmpeg/ffprobe, PySceneDetect and OpenTimelineIO are called through thin adapters. Licensed under MIT.
