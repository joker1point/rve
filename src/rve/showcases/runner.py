"""案例执行器：跑通一个案例，并校验产出是否与期望一致。

流程：load_case → ensure_material → probe_video → detect_shots → build_edit_spec
      → 校验期望指标 → 导出 FCP7 XML → 独立校验器回读 XML。

红线：
- 数值对不上就是 fail，**绝不 quietly succeed**。
- pipeline 相关模块全部**延迟导入**：import 本模块不会拖垮缺依赖的环境。
- 独立校验器（scripts/independent_verify.py）不存在时跳过并在 message 里说明，
  不算失败——它只在仓库检出里可用。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rve.schema.results import ToolReceipt

from .material import MaterialMissing, ensure_material
from .models import CheckResult, ShowcaseCase, ShowcaseRunResult
from .registry import load_case

# duration_sec 的校验容差（秒）
DURATION_TOL = 0.05
# 独立校验器在仓库内的相对位置
_VERIFY_SCRIPT = Path("scripts") / "independent_verify.py"


def _default_workdir(case_id: str) -> Path:
    """默认工作目录：系统临时目录下的 rve-showcases/<case_id>。"""
    return Path(tempfile.gettempdir()) / "rve-showcases" / case_id


def _internal_cut_counts(spec: Any) -> dict[str, int]:
    """统计**内部边界**的转场类型计数。

    内部边界 = 除首镜外的镜头（镜头 0 没有"进入它的转场"），
    故首镜的默认 cut_type 不计入。
    """
    counts: dict[str, int] = {}
    for shot in spec.shots[1:]:
        key = shot.cut_type.value if hasattr(shot.cut_type, "value") else str(shot.cut_type)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _check_expected(case: ShowcaseCase, spec: Any) -> list[CheckResult]:
    """按 case.expected 里出现的键逐项校验，每项产出一条 CheckResult。

    支持的键：shot_count / duration_sec / cut_type_counts / uncertain_count；
    没写的键不校验。
    """
    expected = case.expected
    checks: list[CheckResult] = []

    if "shot_count" in expected:
        want = int(expected["shot_count"])
        got = len(spec.shots)
        checks.append(
            CheckResult(
                name="shot_count",
                passed=got == want,
                expected=str(want),
                actual=str(got),
            )
        )

    if "duration_sec" in expected:
        want = float(expected["duration_sec"])
        got = float(spec.source.duration_sec)
        checks.append(
            CheckResult(
                name="duration_sec",
                passed=abs(got - want) <= DURATION_TOL,
                expected=f"{want:g} (±{DURATION_TOL}s)",
                actual=f"{got:.3f}",
            )
        )

    if "cut_type_counts" in expected:
        counts = _internal_cut_counts(spec)
        for cut_type, want_value in expected["cut_type_counts"].items():
            want = int(want_value)
            got = counts.get(cut_type, 0)
            checks.append(
                CheckResult(
                    name=f"cut_type_counts.{cut_type}",
                    passed=got == want,
                    expected=str(want),
                    actual=str(got),
                )
            )

    if "uncertain_count" in expected:
        want = int(expected["uncertain_count"])
        got = len(spec.uncertain)
        checks.append(
            CheckResult(
                name="uncertain_count",
                passed=got == want,
                expected=str(want),
                actual=str(got),
            )
        )

    return checks


def _find_verify_script() -> Path | None:
    """定位仓库内的独立校验脚本；找不到返回 None。"""
    candidates = [
        Path(__file__).resolve().parents[3] / _VERIFY_SCRIPT,
        Path.cwd() / _VERIFY_SCRIPT,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _independent_verify(xml_path: Path, spec_path: Path) -> tuple[bool, str, bool]:
    """用独立校验器（第二实现）回读导出的 FCP7 XML。

    返回 (是否通过, 说明文字, 是否被跳过)。脚本不存在时返回 (True, 跳过说明, True)——
    跳过不算失败，但必须在 message 里说明。
    """
    script = _find_verify_script()
    if script is None:
        return True, "未找到 scripts/independent_verify.py，跳过独立校验（不算失败）", True
    proc = subprocess.run(
        [sys.executable, str(script), str(xml_path), "--spec", str(spec_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # 独立校验器会打印十几行诊断，整段塞进 CLI/MCP 的单条校验结果里会刷屏：
    # 只保留最后一行裁决，其余细节留在校验器自己的输出里。
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    verdict = lines[-1] if lines else "(无输出)"
    return proc.returncode == 0, f"退出码 {proc.returncode}：{verdict[:200]}", False


async def _analyze(
    material: Path, workdir: Path
) -> tuple[Any, list[str], list[CheckResult], list[str]]:
    """跑通分析链路并导出时间线。

    返回 (spec, 产物清单, 额外校验项, 说明消息)。
    """
    from rve.pipeline.build import build_edit_spec
    from rve.pipeline.detect import detect_shots
    from rve.pipeline.export import export_timeline
    from rve.pipeline.probe import probe_video
    from rve.schema.enums import DetectEngine, ExportFormat
    from rve.schema.results import AudioAnalysisResult
    from rve.workspace import spec_dir

    probe = await probe_video(str(material))
    shots = await detect_shots(str(material), DetectEngine.AUTO, None, 0.0, None)
    # Phase 0：audio 能力未实现，按约定传空实例（与 CLI 的 _cmd_analyze 一致）
    audio = AudioAnalysisResult(receipt=ToolReceipt(tool="noop"))
    built = await build_edit_spec(probe, shots, audio, descriptions=None, spec_id=None)

    xml_path = workdir / f"{built.spec_id}.fcp7xml.xml"
    exported = await export_timeline(
        built.spec_id, ExportFormat.FCP7XML, str(xml_path), None
    )

    spec_path = spec_dir(built.spec_id) / "spec.json"
    artifacts = [str(material), str(spec_path), exported.out_path]
    notes: list[str] = []

    verify_ok, verify_text, skipped = _independent_verify(
        Path(exported.out_path), spec_path
    )
    extra = [
        CheckResult(
            name="independent_verify",
            passed=verify_ok,
            expected="独立校验器 PASS（退出码 0）",
            actual=verify_text,
        )
    ]
    if skipped:
        notes.append(verify_text)
    elif not verify_ok:
        notes.append("独立校验器回读导出的 FCP7 XML 未通过")
    return built.spec, artifacts, extra, notes


def _make_result(
    case_id: str,
    *,
    ok: bool,
    status: str,
    checks: list[CheckResult],
    artifacts: list[str],
    message: str,
    params: dict[str, Any],
    started: datetime,
    t0: float,
    warnings: list[str],
) -> ShowcaseRunResult:
    """组装统一的结果对象（回执与结果同源，便于复核）。"""
    receipt = ToolReceipt(
        tool="rve.showcases.run_case",
        params=params,
        ok=ok,
        started_at=started,
        duration_ms=int((time.perf_counter() - t0) * 1000),
        artifacts=artifacts,
        warnings=warnings,
    )
    return ShowcaseRunResult(
        case_id=case_id,
        ok=ok,
        status=status,
        checks=checks,
        artifacts=artifacts,
        message=message,
        receipt=receipt,
    )


async def run_case(
    case_id: str,
    workdir: Path | None = None,
    material_override: str | None = None,
) -> ShowcaseRunResult:
    """跑一个案例：准备素材 → 走完整分析链路 → 校验期望指标。

    参数:
        case_id: 案例 id（不存在时抛 FileNotFoundError）。
        workdir: 工作目录；None 时用系统临时目录下的 rve-showcases/<case_id>。
        material_override: 外部素材的本地文件路径（跳过自动查找）。

    返回:
        ShowcaseRunResult。status ∈ {pass, fail, material_missing, error}。
        素材缺失与流程异常都被收敛进结果对象（消息里写清原因与下一步），
        绝不悄悄跳过，也绝不把失败报成成功。
    """
    started = datetime.now()
    t0 = time.perf_counter()
    case = load_case(case_id)
    work = Path(workdir) if workdir is not None else _default_workdir(case_id)
    work.mkdir(parents=True, exist_ok=True)
    params: dict[str, Any] = {
        "case_id": case_id,
        "workdir": str(work),
        "material_override": material_override,
        "expected": case.expected,
    }

    try:
        material = ensure_material(case, work, material_override)
    except MaterialMissing as exc:
        return _make_result(
            case_id,
            ok=False,
            status="material_missing",
            checks=[],
            artifacts=[],
            message=str(exc),
            params=params,
            started=started,
            t0=t0,
            warnings=[],
        )

    try:
        spec, artifacts, extra_checks, notes = await _analyze(material, work)
    except Exception as exc:  # noqa: BLE001 — 流程异常要显式可见，不能伪装成通过
        return _make_result(
            case_id,
            ok=False,
            status="error",
            checks=[],
            artifacts=[str(material)],
            message=f"{type(exc).__name__}: {exc}",
            params=params,
            started=started,
            t0=t0,
            warnings=[],
        )

    checks = _check_expected(case, spec) + extra_checks
    ok = all(check.passed for check in checks)
    failed = [check.name for check in checks if not check.passed]
    if ok:
        message = f"案例 {case_id} 校验通过：{len(checks)}/{len(checks)} 项"
    else:
        message = f"案例 {case_id} 校验未通过：不符项 {', '.join(failed)}"
    if notes:
        message = f"{message}（{'; '.join(notes)}）"
    return _make_result(
        case_id,
        ok=ok,
        status="pass" if ok else "fail",
        checks=checks,
        artifacts=artifacts,
        message=message,
        params=params,
        started=started,
        t0=t0,
        warnings=notes,
    )
