# RVE 独立校验与真实 NLE 产物对标发现

## 1. 动机

`scripts/acceptance.py` 的 FCP7 契约断言由**写 XML 的同一份代码**回头校验，属**自证**：
只要导出器与断言共享同一个错误假设，闸门就会"绿"。本工作用两条**独立证据链**补齐：

1. **第二实现**：`scripts/independent_verify.py` 按公开 xmeml 约定从第一性原理重建序列时间轴，
   **只依赖标准库、绝不 import `rve.*`**（`tests/test_independent_verify.py` 用源码正则钉死此约束）。
2. **真实样本对标**：取真实 NLE（Premiere / FCP7 / Resolve 系）导出的 xmeml，
   检验读取器能否吃下真实形态，并列出「真实产物 vs RVE 产物」的结构差异。
   （样本仅存本机临时目录 `%TEMP%\rve_real_samples\`，**不入库**——版权风险。）

## 2. 读取器设计要点

- **纯标准库**：`xml.etree.ElementTree` / `argparse` / `json`。namespace 用本地名比对
  （`_localname`），子元素按**本地名**取而非固定顺序（真实产物子元素顺序不统一）。
- **映射重建**：每个 video `clipitem` 的 `<start>/<end>` 是**序列帧**位置，`<in>/<out>` 是
  **素材内源帧**；据此建立「源帧 → 序列帧」映射。若 `start - in` 跨镜为常数 → 映射为**纯平移**。
- **帧级精度**：`_FRAME_TOL = 0.5`；帧号一律按 `float` 解析（实测真实产物出现 `<out>1055.0</out>`）。
- **哨兵容忍**：真实产物用 `-1` 表示"未设"，`_num` 对负数返回 `None`（不当作有效帧）。
- **主轨启发式**：多轨时优先选"从 0 起且连续铺满整条序列"的轨；否则取 `clipitem` 最多的轨。
- **两种模式**：带 `--spec` 为「RVE 契约模式」（全量断言）；不带为「结构模式」——
  仅当**解析失败 / 根标签非 `xmeml` / 时间倒挂（end<start）**才判失败，其余真实形态一律容忍。

## 3. 真实样本对标：结构差异清单（逐条附证据）

对标样本 9 份（Premiere 5 份、FCP7/Hiero 1 份、第三方金标 3 份）。**读取器 9/9 全部 PASS（RC=0）**。

| # | 差异点 | 真实产物（证据） | RVE 产物现状 | 影响 |
|---|--------|------------------|--------------|------|
| 1 | `clipitem/<duration>` 语义 | **真实产物本身口径不一致**：有的写素材总时长（`opentake` dur=120 而 out-in=30；`doublelove` dur=250 而 out-in=22）；有的写本段长度（`otiofcp` dur=50 **== out-in 50**，同 clip 的 `file/<duration>=100`） | 写 **out-in**（本段长度），且恒有 `dur == end-start == out-in` | **未定论**：RVE 口径与「本段长度」派真实样本一致；该字段在真实 NLE 产物中本就没有统一口径 |
| 2 | `-1` 未设哨兵 | `otiofcp` `<start>322</start><end>-1</end>`；`<in>73</in><out>-1</out>` | 从不出现 | 读取器须容忍，否则误判 |
| 3 | per-clipitem `<rate>` | 素材原生帧率，可 ≠ 序列帧率（`opentake` ci rate=30 但 file rate=60） | 无 per-clip `<rate>` | 帧率换算可能失真 |
| 4 | 多轨 + 轨内空洞/非 0 起点 | `opentake` 3 条 video 轨（2 空轨 + 1 有 content）；`otiofcp` 单轨内 clip 从 536 起 | 仅 1 条 video 轨、从 0 连续铺满 | RVE 形态是**合法子集** |
| 5 | `<!DOCTYPE xmeml>` | 9 份中普遍存在（`doublelove`/`otiofcp`/…） | 无 DOCTYPE | 多数 NLE 仍接受；低风险 |
| 6 | `<timecode>` 结构 | `doublelove`：含 `<source>`、**无 `<string>`**；`opentake`：`<string>`+`<source>` 皆有 | 有 `<string>`、**无 `<source>`** | 真实产物 `<source>` 普遍 |
| 7 | 其它形态 | `hiero` `version="5"`；`doublelove` 用 `<file .../>` 自闭合二次引用；音频 clipitem 挂 `<file>`+`<link>` 与视频互链 | `version="4"`；音频 clipitem 现已挂**真实 `<file>`**（本轮补齐，替换原骨架），但**无 `<link>` 与视频互链** | 音频互链（`<link>`）仍是潜在缺口 |

## 4. 由此确认 / 推翻的 RVE 结构缺口

- **确认缺口（低危，建议后续修）**
  - **#1 口径未定论（非确认缺陷）**：RVE 把 `<clipitem><duration>` 写成"本段长度"（== `out-in` == `end-start`），
    这与真实样本中的「本段长度」派一致（`otiofcp` dur=50 == out-in=50）；但另有真实样本写"素材总时长"
    （`opentake` dur=120、`doublelove` dur=250）。**该字段在真实 NLE 产物中口径不统一**，
    故不能据此判定 RVE 不合规；**结论改为「与规范/某派真实产物一致，是否被具体 NLE 接受未实测」**。
    证据复核命令：对 `%TEMP%\rve_real_samples\` 下样本逐个打印 start/end/in/out/duration 即可复现。
  - **#6 `<source>` 缺失**：RVE `<timecode>` 无 `<source>` 子元素，真实产物普遍有。低危（多数 NLE 容忍）；
    **未实测**，故本轮不改（凭猜添加元素的风险高于收益）。
  - **#7 音频缺 `<link>` 互链**：音频 clipitem 现已挂真实 `<file>`，但**未写 `<link>` 与视频互链**；
    真实产物普遍有 `<link>`。是否必需**未实测**。
- **推翻 / 确认无缺口**
  - **多轨与空洞**：真实产物允许（#4），RVE 只输出单轨从 0 铺满——这是**合法子集**，不算缺陷。
  - **`<!DOCTYPE>` / `version=4`**：真实样本读入无障碍；RVE 无 DOCTYPE 仍被读取器与验收接受，
    **未发现由此导致的必败缺陷**。
  - **帧号/`-1`/浮点**：属**读取器**需容忍的输入形态，与 RVE 输出无关。

## 5. 真实产物裁决（本次实测）

- **RVE 真实产物**：`20261019-596a0f.fcp7xml.xml`（10 镜 / 24fps / seq duration=1483）
  + 冻结 `spec.json` → **PASS（RC=0）**。映射纯平移（偏移≈0 跨 10 镜）、时间轴连续无空洞/重叠、
  duration 与期望一致、转场数一致、`<timecode>` 存在、音频轨存在。
- **9 份真实 NLE 样本**（结构模式）→ **9/9 PASS（RC=0）**，读取器可吃下真实形态。
- **独立测试**：`tests/test_independent_verify.py` **12 passed**（含 CLI 子进程现生成产物读回、
  手工坏 XML 空洞/重叠必须失败、CLI 非零退出、真实样本可解析）。

## 6. 无法验证的部分（诚实列出）

1. **真实 NLE 导入实测未闭环**：以上差异（#1/#6/#7）对 Premiere/Resolve **实际导入行为**的影响
   未经真人实测——只能从 XML 结构与公开约定推断。
2. **样本代表性有限**：9 份样本来自公开仓库，非主流商用 NLE 的全覆盖；
   Resolve 导出的 xmeml 未纳入。
3. **转场语义**：本次真实样本均**无 `transitionitem`**，转场严格跨骑的判定只在 RVE 自产 XML +
   手工夹具上验证，未与真实转场产物对标。
4. **`<source>`/音频互链的后果**未知，仅标注为潜在缺口。
