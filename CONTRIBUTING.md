# 贡献指南

感谢参与 RVE。动手前请先读本页，避免返工。

## 开发环境

```bash
git clone <repo-url> rve && cd rve
python -m pip install -e ".[dev]"
```

前置条件：本机需装 **ffmpeg（含 ffprobe）** 并加入 `PATH`。用 `rve doctor` 确认环境。

## 跑测试

```bash
python -m pytest -q
```

- `tests/test_probe_detect.py` 依赖 `ffprobe` 与 `scenedetect`；**缺失时会 skip 而非失败**，所以本地"绿"可能是假绿——动手前先确认 `rve doctor` 里这两项都 `[OK]`。
- CI（`.github/workflows/ci.yml`）已在 ubuntu-latest 上装好 ffmpeg，跑 Python 3.11/3.12/3.13 矩阵；提交前请在本地尽量对齐。

## 提 PR 的顺序

1. **先沟通占坑**：大改动先开 issue 说明意图与方案，得到认可再动手，避免"改动过大"被拒。
2. **小步提交**：一个 PR 只做一件事，附带复现步骤与测试输出。
3. **快速响应 review**：尽量在 24h 内回复评论。
4. **改动自带验证**：对外宣称的能力，必须对应一条可复现的命令（README 同理）。

## 硬性约定

- **不 vendor 上游源码**：FFmpeg/ffprobe、PySceneDetect、OpenTimelineIO 只通过薄适配器调用，不复制其代码。
- **EditSpec 契约不可松**：`extra="forbid"`、纯秒锚定、镜头连续且铺满时间轴的不变量必须保持；时间码换算只写在 `src/rve/exporters/`。
- **失败不吞**：未实现的能力必须显式报错（`NotImplementedError`），绝不 quietly succeed。

## 目录约定

- `src/rve/schema/`：唯一权威结构与受控词表（结构取值只来自 `enums.py`）。
- `src/rve/pipeline/`：流水线步骤（probe/detect/build/export/report/…）。
- `src/rve/providers/`：可替换后端（探针、切镜）及其 Protocol 契约。
- `src/rve/exporters/`：秒↔时间码换算与各 NLE 格式写出。
- `tests/`：契约测试与端到端测试；`pytest` 只收集 `tests/`。
