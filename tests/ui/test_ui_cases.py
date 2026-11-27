# -*- coding: utf-8 -*-
"""UI 用例数据驱动执行器。

ui_mode=ego_skill：整条用例通过 ego-browser CLI + JS 执行；
其余模式：Playwright DSL 执行，skill 动作需切到 ego_skill 模式。
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:
    pytest.exit("缺少依赖 PyYAML，请执行: pip install PyYAML", returncode=1)

WORKFLOW_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKFLOW_ROOT / "scripts"))
from dsh_common import mask_sensitive, safe_case_id, temp_root, selected_case_files  # noqa: E402

SCREENSHOT_DIR = temp_root() / "screenshots"
CASE_DIR = WORKFLOW_ROOT / "cases" / "ui"
UI_BASE_URL = os.environ.get("DSH_UI_BASE_URL", "http://localhost:3000")
UI_TIMEOUT = float(os.environ.get("DSH_UI_TIMEOUT", "10"))
EGO_MODE = os.environ.get("DSH_EGO_MODE", "").lower() == "ego_skill" \
    or os.environ.get("DSH_UI_MODE", "").lower() == "ego_skill"
IGNORE_FILES = {"example.ui.yaml"}


def _collect_cases():
    cases = []
    if not CASE_DIR.exists():
        return cases
    for path in selected_case_files(CASE_DIR):
        if path.name in IGNORE_FILES:
            continue
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("case_id"):
                cases.append((item["case_id"], item))
    return cases


CASES = _collect_cases()
if not CASES:
    pytest.skip("cases/ui 下没有可用用例", allow_module_level=True)


def _now():
    return datetime.now().isoformat(timespec="milliseconds")


def _write_runtime(ctx: dict):
    run_dir = os.environ.get("DSH_RUN_DIR")
    if not run_dir:
        return
    runtime_dir = Path(run_dir) / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / f"ui-{safe_case_id(ctx['case_id'])}.json"
    path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(WORKFLOW_ROOT))
    except ValueError:
        return str(path)


def _skip_if_manual(case_id: str, case: dict) -> None:
    # 显式声明跳过：前提在本环境无法复现，跳过比记成失败更接近事实
    if case.get("skip"):
        pytest.skip(f"[{case_id}] {case.get('skip_reason') or '按配置跳过'}")
    if case.get("needs_manual_context"):
        reasons = ", ".join(case.get("manual_context_reasons") or [])
        pytest.skip(f"[{case_id}] 需人工浏览器上下文/路由模拟，自动执行跳过: {reasons}")


def _resolve_url(target: str) -> str:
    if target.startswith("http://") or target.startswith("https://"):
        return target
    return UI_BASE_URL.rstrip("/") + "/" + target.lstrip("/")


if EGO_MODE:
    from ego_client import run_case as ego_run_case  # noqa: E402

    @pytest.mark.parametrize("case_id,case", CASES, ids=[c[0] for c in CASES])
    def test_ui_case_ego(case_id, case):
        _skip_if_manual(case_id, case)
        result = ego_run_case(
            case_id=case_id,
            steps=case.get("steps") or [],
            base_url=UI_BASE_URL,
            run_id=os.environ.get("DSH_RUN_ID", ""),
            screenshot_dir=str(SCREENSHOT_DIR),
        )
        result["run_id"] = result.get("run_id") or os.environ.get("DSH_RUN_ID", "")
        _write_runtime(result)
        if result.get("status") != "passed":
            pytest.fail(f"[{case_id}] {result.get('error', 'EGO 执行失败')}", pytrace=False)

else:
    playwright = pytest.importorskip(
        "playwright", reason="未安装 playwright 库，UI 测试跳过: pip install playwright"
    )

    def _run_step(page, action: str) -> str:
        parts = str(action).strip().split(" ", 2)
        verb = parts[0].lower() if parts else ""
        arg1 = parts[1] if len(parts) > 1 else ""
        arg2 = parts[2] if len(parts) > 2 else ""

        if verb == "goto":
            page.goto(_resolve_url(arg1), timeout=UI_TIMEOUT * 1000)
        elif verb == "click":
            page.click(arg1, timeout=UI_TIMEOUT * 1000)
        elif verb == "fill":
            page.fill(arg1, arg2, timeout=UI_TIMEOUT * 1000)
        elif verb == "press":
            page.press(arg1, arg2, timeout=UI_TIMEOUT * 1000)
        elif verb == "check":
            page.check(arg1, timeout=UI_TIMEOUT * 1000)
        elif verb == "select":
            page.select_option(arg1, arg2, timeout=UI_TIMEOUT * 1000)
        elif verb == "hover":
            page.hover(arg1, timeout=UI_TIMEOUT * 1000)
        elif verb == "wait":
            time.sleep(float(arg1 or 1))
        elif verb == "screenshot":
            out = SCREENSHOT_DIR / f"{safe_case_id(arg1 or 'shot')}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), full_page=True)
        elif verb == "expect_url_contains":
            assert arg1 in page.url, f"期望 URL 包含 `{arg1}`，实际 {page.url}"
        elif verb == "expect_text":
            actual = page.inner_text(arg1, timeout=UI_TIMEOUT * 1000)
            assert arg2 in actual, f"元素 `{arg1}` 中未找到文本 `{arg2}`"
        elif verb == "expect_visible":
            assert page.is_visible(arg1, timeout=UI_TIMEOUT * 1000), f"元素 `{arg1}` 不可见"
        elif verb == "skill":
            raise RuntimeError("skill 动作需在 config/workflow.yaml 中设置 ui_mode: ego_skill")
        else:
            raise ValueError(f"未知 UI 动作: `{verb}`（步骤: {action}）")
        return f"{verb} ok"

    @pytest.mark.parametrize("case_id,case", CASES, ids=[c[0] for c in CASES])
    def test_ui_case(page, case_id, case):
        _skip_if_manual(case_id, case)
        ctx = {
            "run_id": os.environ.get("DSH_RUN_ID", ""),
            "case_id": case_id,
            "status": "passed",
            "start_time": _now(),
            "steps": [],
            "assert_detail": [],
            "skill_results": [],
            "screenshot_path": "",
        }
        started = time.time()
        try:
            for idx, step in enumerate(case.get("steps") or [], 1):
                action = step.get("action", "") if isinstance(step, dict) else str(step)
                try:
                    result = _run_step(page, str(action))
                    ctx["steps"].append({"step": idx, "action": str(action), "result": result})
                except Exception as e:
                    shot_dir = SCREENSHOT_DIR
                    shot_dir.mkdir(parents=True, exist_ok=True)
                    shot = shot_dir / f"fail-{safe_case_id(case_id)}-step{idx}.png"
                    try:
                        page.screenshot(path=str(shot), full_page=True)
                        ctx["screenshot_path"] = _rel(shot)
                    except Exception:
                        pass
                    pytest.fail(
                        f"[{case_id}] 步骤 {idx} `{action}` 失败: {e}"
                        + (f"（截图: {ctx['screenshot_path']}）" if ctx.get("screenshot_path") else ""),
                        pytrace=False,
                    )
        except BaseException as e:
            if ctx["status"] == "passed":
                ctx["status"] = "failed"
                ctx["error_msg"] = str(e)[:2000]
            raise
        finally:
            ctx["end_time"] = _now()
            ctx["cost_ms"] = int((time.time() - started) * 1000)
            ctx["req_content"] = json.dumps(case.get("steps") or [], ensure_ascii=False)
            ctx["resp_content"] = json.dumps(ctx, ensure_ascii=False)
            _write_runtime(ctx)
