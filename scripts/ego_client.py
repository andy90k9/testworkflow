#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EGO Browser Skill 适配器。

通过本地 CLI `ego-browser nodejs` 执行 JS 片段驱动 ego lite 浏览器，
不使用 HTTP 服务。运行时指南由 `ego-browser skill` 打印并缓存。
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import load_providers, resolve, safe_case_id, WORKFLOW_ROOT  # noqa: E402

GUIDE_END_MARK = "[ego-browser:skill] end of guide"


def _ego_cfg() -> dict:
    return load_providers().get("ego_browser") or {}


def cli_path() -> str:
    return os.environ.get("DSH_EGO_CLI") or _ego_cfg().get("cli_path") or "ego-browser"


def guide_path() -> Path:
    raw = os.environ.get("DSH_EGO_GUIDE_CACHE") or _ego_cfg().get("skill_guide_cache") \
        or "records/ego-browser-skill-guide.md"
    return resolve(raw)


def timeout_sec() -> int:
    try:
        return int(os.environ.get("DSH_EGO_TIMEOUT") or _ego_cfg().get("nodejs_timeout_sec") or 30)
    except (TypeError, ValueError):
        return 30


def check_installed() -> str | None:
    return shutil.which(cli_path())


def refresh_guide() -> str:
    """执行 ego-browser skill 刷新运行时指南缓存。"""
    exe = check_installed()
    if not exe:
        raise RuntimeError(f"未找到 {cli_path()}，请先安装 ego-browser skill 与 ego lite 浏览器")
    proc = subprocess.run([exe, "skill"], capture_output=True, text=True, timeout=timeout_sec())
    if proc.returncode != 0:
        raise RuntimeError(f"ego-browser skill 返回 {proc.returncode}: {(proc.stderr or proc.stdout)[-1000:]}")
    if GUIDE_END_MARK not in proc.stdout:
        raise RuntimeError("ego-browser skill 输出被截断（缺少 end of guide 标记），请重定向到文件后重试")
    path = guide_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(proc.stdout, encoding="utf-8")
    return proc.stdout


def get_guide() -> str:
    """获取运行时指南；配置 auto_refresh_guide=false 时使用本地缓存。"""
    ego = _ego_cfg()
    path = guide_path()
    if ego.get("auto_refresh_guide", True) or not path.exists():
        return refresh_guide()
    return path.read_text(encoding="utf-8")


def guide_version(guide: str) -> str:
    m = re.search(r"end of guide\s+(v[0-9.]+)", guide)
    return m.group(1) if m else "unknown"


def _run_js(js: str) -> str:
    exe = check_installed()
    if not exe:
        raise RuntimeError(f"未找到 {cli_path()}，无法执行 UI 用例")
    if os.name == "nt":
        cmd = [exe, "nodejs", "-e", js]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec())
    else:
        cmd = [exe, "nodejs"]
        proc = subprocess.run(cmd, input=js, capture_output=True, text=True, timeout=timeout_sec())
    if proc.returncode != 0:
        raise RuntimeError(f"ego-browser nodejs 返回 {proc.returncode}: {(proc.stderr or proc.stdout)[-2000:]}")
    return proc.stdout


def _parse_result(stdout: str) -> dict:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") or line.startswith("["):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"ego-browser nodejs 未输出 JSON 结果: {stdout[-1000:]}")


def _js(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _emit_action(action: str, idx: int, base_url: str, shot_dir: Path,
                 case_id: str, whitelist: set, timeout_ms: int) -> list:
    """把一条 UI DSL 翻译成 ego-browser JS 片段。"""
    parts = str(action).strip().split(" ", 2)
    verb = parts[0].lower() if parts else ""
    arg1 = parts[1] if len(parts) > 1 else ""
    arg2 = parts[2] if len(parts) > 2 else ""
    lines = [f"currentStep = {idx};"]
    cid = safe_case_id(case_id)

    def step_ok(result: str = "ok"):
        lines.append(f"report.steps.push({{ step: {idx}, action: {_js(action)}, result: {_js(result)} }});")

    if verb == "goto":
        url = arg1 if arg1.startswith(("http://", "https://")) else base_url.rstrip("/") + "/" + arg1.lstrip("/")
        lines.append(f"await page.goto({_js(url)});")
        lines.append("await page.waitForLoadState();")
        step_ok()
    elif verb == "click":
        lines.append(f"await page.click({_js(arg1)}, {{ label: 'dsh click step {idx}' }});")
        step_ok()
    elif verb == "hover":
        lines.append(f"await page.hover({_js(arg1)}, {{ label: 'dsh hover step {idx}' }});")
        step_ok()
    elif verb == "fill":
        lines.append(f"await page.fill({_js(arg1)}, {_js(arg2)});")
        step_ok()
    elif verb == "press":
        lines.append(f"await page.press({_js(arg1)}, {_js(arg2)});")
        step_ok()
    elif verb == "check":
        lines.append(f"await page.click({_js(arg1)}, {{ label: 'dsh check step {idx}' }});")
        step_ok()
    elif verb == "select":
        lines.append(f"await page.selectOption({_js(arg1)}, {_js(arg2)});")
        step_ok()
    elif verb == "wait":
        lines.append(f"await page.waitForTimeout({int(float(arg1 or 1) * 1000)});")
        step_ok()
    elif verb == "screenshot":
        path = (shot_dir / f"{cid}-{safe_case_id(arg1 or 'shot')}.png").resolve()
        lines.append(f"report.screenshot_path = await page.screenshot({{ path: {_js(str(path))} }});")
        step_ok("screenshot")
    elif verb == "expect_url_contains":
        lines.append(f"const u{idx} = await page.url();")
        lines.append(f"if (!u{idx}.includes({_js(arg1)})) {{ throw new Error('期望 URL 包含 ' + {_js(arg1)} + '，实际 ' + u{idx}); }}")
        lines.append(f"report.assert_detail.push({{ name: 'expect_url_contains', expected: {_js(arg1)}, actual: u{idx}, passed: true }});")
        step_ok("assert")
    elif verb == "expect_text":
        lines.append("const snap = await page.snapshot({ scope: 'full_page' });")
        lines.append(f"if (!snap.includes({_js(arg2)})) {{ throw new Error({_js(f'期望页面包含文本 {arg2}')}); }}")
        lines.append(f"report.assert_detail.push({{ name: 'expect_text', expected: {_js(arg2)}, actual: snap.slice(0, 500), passed: true }});")
        step_ok("assert")
    elif verb == "expect_visible":
        lines.append(f"await page.waitForSelector({_js(arg1)}, {{ state: 'visible', timeout: {timeout_ms} }});")
        lines.append(f"report.assert_detail.push({{ name: 'expect_visible', expected: {_js(arg1)}, actual: 'visible', passed: true }});")
        step_ok("assert")
    elif verb == "skill":
        if len(parts) < 2 or not arg1:
            raise ValueError("skill 动作缺少技能名")
        if whitelist and arg1 not in whitelist:
            raise ValueError(f"技能 `{arg1}` 不在 ego_browser.skill_whitelist 中")
        try:
            args = json.loads(arg2) if arg2.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(f"skill 参数不是合法 JSON: {e}")
        lines.extend(_emit_skill(arg1, args, idx, shot_dir, cid))
        step_ok("skill:" + arg1)
    else:
        raise ValueError(f"未知 UI 动作 `{verb}`（步骤: {action}）")
    return lines


def _emit_skill(skill: str, args: dict, idx: int, shot_dir: Path, cid: str) -> list:
    """把白名单技能翻译成 ego-browser API 调用。"""
    lines = [f"const a{idx} = {_js(args)};"]
    if skill in ("snapshot", "dom_inspect"):
        lines.append(f"const r{idx} = await page.snapshot({{ scope: 'full_page' }});")
        lines.append(f"report.skill_results.push({{ skill: {_js(skill)}, args: a{idx}, result: r{idx} }});")
    elif skill in ("capture", "screenshot"):
        path = (shot_dir / f"{cid}-skill-{idx}.png").resolve()
        lines.append(f"const r{idx} = await page.screenshot({{ path: {_js(str(path))} }});")
        lines.append(f"report.screenshot_path = r{idx};")
        lines.append(f"report.skill_results.push({{ skill: {_js(skill)}, args: a{idx}, result: r{idx} }});")
    elif skill == "evaluate":
        script = args.get("script") or args.get("expression") or "return null"
        lines.append(f"const r{idx} = await page.evaluate({_js(script)}, a{idx}.arg);")
        lines.append(f"report.skill_results.push({{ skill: 'evaluate', args: a{idx}, result: r{idx} }});")
    elif skill == "extract":
        selector = args.get("selector", "body")
        limit = int(args.get("limit", 20))
        lines.append(
            "const r" + str(idx) + " = await page.evaluate("
            "({ selector, limit }) => [...document.querySelectorAll(selector)].slice(0, limit)"
            ".map((n) => ({ text: n.textContent?.trim() || '', href: n.querySelector('a')?.href || '' })), "
            "{ selector: " + _js(selector) + ", limit: " + str(limit) + " });"
        )
        lines.append(f"report.skill_results.push({{ skill: 'extract', args: a{idx}, result: r{idx} }});")
    elif skill == "fetch":
        lines.append(f"const r{idx} = await page.fetch(a{idx}.url, a{idx}.options || {{}});")
        lines.append(f"report.skill_results.push({{ skill: 'fetch', args: a{idx}, result: r{idx} }});")
    elif skill == "cdp":
        target = str(args.get("target", "page")).lower()
        obj = "task" if target == "task" else "page"
        lines.append(f"const r{idx} = await {obj}.cdp(a{idx}.method, a{idx}.params || {{}});")
        lines.append(f"report.skill_results.push({{ skill: 'cdp', args: a{idx}, result: r{idx} }});")
    elif skill in ("wait_for", "wait_for_selector"):
        lines.append(f"await page.waitForSelector(a{idx}.selector, {{ state: a{idx}.state || 'visible', timeout: a{idx}.timeout || 30000 }});")
        lines.append(f"report.skill_results.push({{ skill: {_js(skill)}, args: a{idx}, result: 'ok' }});")
    elif skill == "handoff":
        lines.append("await task.handOff();")
        lines.append(f"report.skill_results.push({{ skill: 'handoff', args: a{idx}, result: 'user control' }});")
    else:
        raise ValueError(f"暂不支持的 EGO 技能 `{skill}`，请查看运行时指南: {guide_path()}")
    return lines


def run_case(case_id: str, steps: list, base_url: str = "", run_id: str = "",
             screenshot_dir: str | None = None) -> dict:
    """用 ego-browser CLI 执行一条 UI 用例，返回 runtime 明细。"""
    guide = get_guide()
    ego = _ego_cfg()
    whitelist = set(ego.get("skill_whitelist") or [])
    shot_dir = Path(screenshot_dir) if screenshot_dir else WORKFLOW_ROOT / "records" / "screenshots"
    shot_dir = shot_dir.resolve()
    shot_dir.mkdir(parents=True, exist_ok=True)
    timeout_ms = timeout_sec() * 1000

    js_lines = [
        f"const SHOT_DIR = {_js(str(shot_dir))};",
        "const report = { status: 'passed', steps: [], assert_detail: [], skill_results: [], screenshot_path: '' };",
        "let currentStep = 0;",
        f"const task = await taskSpace({_js('dsh-' + safe_case_id(run_id or 'run') + '-' + safe_case_id(case_id))});",
        "const page = task.page('p1');",
        "try {",
    ]
    for idx, step in enumerate(steps or [], 1):
        action = step.get("action", "") if isinstance(step, dict) else str(step)
        js_lines.extend(_emit_action(str(action), idx, base_url, shot_dir, case_id, whitelist, timeout_ms))
    js_lines.extend([
        "  console.log(JSON.stringify({ ...report, case_id: CASE_ID, run_id: RUN_ID, guide_version: GUIDE_VERSION, spaceId: task.spaceId }));",
        "} catch (e) {",
        "  report.status = 'failed';",
        "  report.error = String(e && e.message ? e.message : e);",
        f"  const shot = SHOT_DIR + '/fail-{safe_case_id(case_id)}-step' + currentStep + '.png';",
        "  try { report.screenshot_path = await page.screenshot({ path: shot }); } catch (_) {}",
        "  console.log(JSON.stringify({ ...report, case_id: CASE_ID, run_id: RUN_ID, guide_version: GUIDE_VERSION, spaceId: task.spaceId }));",
        "}",
    ])
    script = "\n".join(js_lines)
    script = script.replace("CASE_ID", _js(case_id)).replace("RUN_ID", _js(run_id)) \
                   .replace("GUIDE_VERSION", _js(guide_version(guide)))

    result = _parse_result(_run_js(script))
    result["req_content"] = json.dumps(steps or [], ensure_ascii=False)
    result["resp_content"] = json.dumps(
        {k: v for k, v in result.items() if k not in ("resp_content",)}, ensure_ascii=False)

    space_id = result.get("spaceId")
    if space_id is not None:
        try:
            _run_js(f"const task = await taskSpace({_js(space_id)}); await task.finish({{ keep: [] }}); "
                    "console.log(JSON.stringify({ finished: true }));")
        except Exception as e:
            result["finish_error"] = str(e)[:500]
    return result
