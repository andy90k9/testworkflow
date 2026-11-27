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
from dsh_common import load_providers, resolve, safe_case_id, temp_root, WORKFLOW_ROOT  # noqa: E402

GUIDE_END_MARK = "[ego-browser:skill] end of guide"


def _ego_cfg() -> dict:
    return load_providers().get("ego_browser") or {}


def cli_path() -> str:
    return os.environ.get("DSH_EGO_CLI") or _ego_cfg().get("cli_path") or "ego-browser"


def guide_path() -> Path:
    raw = os.environ.get("DSH_EGO_GUIDE_CACHE") or _ego_cfg().get("skill_guide_cache") \
        or str(temp_root() / "ego-browser-skill-guide.md")
    return resolve(raw)


def timeout_sec() -> int:
    try:
        return int(os.environ.get("DSH_EGO_TIMEOUT") or _ego_cfg().get("nodejs_timeout_sec") or 30)
    except (TypeError, ValueError):
        return 30


def assert_timeout_ms() -> int:
    """断言等待上限，留出进程收尾时间，避免子进程超时吞掉断言结果。"""
    proc = timeout_sec()
    try:
        configured = int(os.environ.get("DSH_EGO_ASSERT_TIMEOUT")
                         or _ego_cfg().get("assert_timeout_sec") or max(1, proc // 3))
    except (TypeError, ValueError):
        configured = max(1, proc // 3)
    return min(configured, max(1, proc - 5)) * 1000


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
    # ego-browser 版本差异：console.log 的 JSON 可能落在 stdout 或 stderr，合并后由 _parse_result 取最后一段 JSON。
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


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


def _split_action(action: str) -> tuple:
    """拆分 `verb selector [value]`，保留选择器内的 `>> nth=N` 与引号内空格。

    普通 split(" ") 会把 `[data-testid="x"] >> nth=0` 拆成两段，
    导致选择器被截断、nth 丢失，多元素命中的用例全部失败。
    """
    text = str(action).strip()
    parts = text.split(" ", 2)
    verb = parts[0].lower() if parts else ""
    if len(parts) < 2:
        return verb, "", ""
    rest = text[len(parts[0]):].lstrip()
    if verb in ("mock_route", "mock_stream"):
        # 第一个 token 是 URL 片段，其余整体是状态码+响应体 / SSE 文本
        idx = rest.find(" ")
        if idx > 0:
            return verb, rest[:idx].strip(), rest[idx + 1:].strip()
        return verb, rest, ""
    if verb in ("mock_off",):
        return verb, rest.strip(), ""
    if verb in ("fill", "expect_text", "upload", "download", "press", "select"):
        # 这些动作的最后一段是取值，选择器允许含空格（>> nth=N）
        idx = rest.rfind(" ")
        if idx > 0:
            return verb, rest[:idx].strip(), rest[idx + 1:].strip()
        return verb, rest, ""
    return verb, rest.strip(), ""


SESSION_BOOTSTRAP_JS = """
try {
  const signedIn = await page.evaluate(() => !!document.querySelector('[data-testid="signed-in-user"]'));
  if (!signedIn && PAGE_SESSION) {
    await page.cdp('Page.addScriptToEvaluateOnNewDocument', { source: PAGE_SESSION });
    await page.reload();
    await page.waitForLoadState();
    await new Promise((r) => setTimeout(r, 1500));
    const ok = await page.evaluate(() => !!document.querySelector('[data-testid="signed-in-user"]'));
    report.sessionBootstrap = ok ? 'ok' : 'failed';
  } else if (signedIn) {
    report.sessionBootstrap = 'existing';
  }
} catch (e) { report.sessionBootstrapError = String(e).slice(0, 300); }
""".strip().splitlines()


def _page_session_source() -> str:
    """用已渲染的 API token 构造前端 portal 会话。

    浏览器 SSO 会话会过期（跳 sso-test 登录页），而 API token 仍然有效；
    前端把会话放在 localStorage 的 `ai-center-portal:session`，
    结构为 { accessToken, expiresAt, user: { userId, userName } }。
    """
    token = ""
    for path in (os.environ.get("DSH_API_TOKEN_FILE"), os.environ.get("DSH_TOKEN_FILE")):
        if path and Path(path).expanduser().is_file():
            try:
                token = Path(path).expanduser().read_text(encoding="utf-8").strip()
            except Exception:
                token = ""
            if token:
                break
    if not token:
        return ""
    payload = (
        "(() => { try { localStorage.setItem('ai-center-portal:session', JSON.stringify({"
        f"accessToken: {json.dumps(token)}, expiresAt: Date.now() + 3600 * 1000, "
        "user: { userId: '905', userName: 'auto' } })); } catch (e) {} })();"
    )
    return payload


def _emit_action(action: str, idx: int, base_url: str, shot_dir: Path,
                 case_id: str, whitelist: set, timeout_ms: int,
                 has_mock: bool = False) -> list:
    """把一条 UI DSL 翻译成 ego-browser JS 片段。"""
    verb, arg1, arg2 = _split_action(action)
    lines = [f"currentStep = {idx};"]
    cid = safe_case_id(case_id)

    def step_ok(result: str = "ok"):
        lines.append(f"report.steps.push({{ step: {idx}, action: {_js(action)}, result: {_js(result)} }});")

    if verb == "goto":
        url = arg1 if arg1.startswith(("http://", "https://")) else base_url.rstrip("/") + "/" + arg1.lstrip("/")
        lines.append(f"await page.goto({_js(url)});")
        lines.append("await page.waitForLoadState();")
        if has_mock:
            # mock 是"新文档生效"注入：同 URL 的 hash 跳转不产生新文档，
            # 必须强制 reload 一次，注入才会生效（复用的已登录空间里尤其常见）。
            lines.append("await page.reload();")
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
        needle = arg2
        lines.append(f"const snap{idx} = await page.snapshot({{ scope: 'full_page' }});")
        lines.append(f"const needle{idx} = {_js(needle)};")
        lines.append(f"const pos{idx} = snap{idx}.indexOf(needle{idx});")
        lines.append(f"if (pos{idx} < 0) {{ throw new Error({_js(f'期望页面包含文本 {needle}')}); }}")
        lines.append(
            f"report.assert_detail.push({{ name: 'expect_text', expected: needle{idx}, "
            f"actual: snap{idx}.slice(Math.max(0, pos{idx} - 120), pos{idx} + needle{idx}.length + 180), "
            f"match_pos: pos{idx}, snapshot_len: snap{idx}.length, passed: true }});"
        )
        step_ok("assert")
    elif verb == "expect_visible":
        lines.append(f"await page.waitForSelector({_js(arg1)}, {{ state: 'visible', timeout: {timeout_ms} }});")
        lines.append(f"report.assert_detail.push({{ name: 'expect_visible', expected: {_js(arg1)}, actual: 'visible', passed: true }});")
        step_ok("assert")
    elif verb in ("mouse_click", "mouse_move", "mouse_wheel"):
        # 用法: mouse_click 420,260 [说明] / mouse_move 420,260 / mouse_wheel 0,600
        try:
            x, y = (int(v.strip()) for v in arg1.split(",", 1))
        except (ValueError, TypeError):
            raise ValueError(f"mouse 动作坐标格式错误: {arg1}（应为 x,y）")
        label = arg2 or f"dsh {verb} step {idx}"
        call = {"mouse_click": f"click({x}, {y}", "mouse_move": f"move({x}, {y}",
                "mouse_wheel": f"wheel({x}, {y}"}[verb]
        lines.append(f"await page.mouse.{call}, {{ label: {_js(label)} }});")
        step_ok(verb)
    elif verb in ("key_press", "type_text", "paste", "insert_text"):
        method = {"key_press": "press", "type_text": "type",
                  "paste": "paste", "insert_text": "insertText"}[verb]
        value = arg1 if verb == "key_press" else " ".join(p for p in (arg1, arg2) if p)
        lines.append(f"await page.keyboard.{method}({_js(value)});")
        step_ok(verb)
    elif verb == "reload":
        lines.append("await page.reload();")
        lines.append("await page.waitForLoadState();")
        step_ok("reload")
    elif verb == "mock_off":
        # 用法: mock_off <url包含串>
        # 关闭指定 mock（等价 Playwright 的 page.unroute）：在页面内把命中上限设为当前次数，
        # 后续请求回落真实后端。CDP 注入本身无法撤销，故用"停用"实现。
        needle = arg1
        lines.append(
            "await page.evaluate((n) => { (window.__dsh_mocks || []).forEach((m) => {"
            " if (m.needle.indexOf(n) >= 0 || n.indexOf(m.needle) >= 0) m.limit = m.hits; }); },"
            f" {_js(needle)});"
        )
        step_ok("mock_off")
    elif verb == "mock_route":
        # 用法: mock_route <url包含串> <状态码> <响应体JSON> [次数]
        # 用 CDP addScriptToEvaluateOnNewDocument 注入 fetch 覆盖，
        # 该注入在 reload / 首次导航后依然生效，等价于 Playwright 的 page.route。
        # 可选的第 4 段为命中次数上限（模拟 unroute：用满后回落真实请求）。
        needle = arg1
        rest = str(arg2 or "").split(" ", 2)
        try:
            status = int(rest[0]) if rest and rest[0] else 200
        except ValueError:
            raise ValueError(f"mock_route 状态码需为数字: {rest[0] if rest else arg2}")
        payload = rest[1] if len(rest) > 1 and rest[1] else '{"code":500,"msg":"mock"}'
        try:
            limit = int(rest[2]) if len(rest) > 2 and rest[2].strip() else 0
        except ValueError:
            limit = 0
        source = (
            "(() => { const orig = window.fetch; window.__dsh_mocks = window.__dsh_mocks || [];"
            f" const entry = {{needle: {_js(needle)}, status: {status}, body: {_js(payload)},"
            f" limit: {limit}, hits: 0}};"
            " window.__dsh_mocks.push(entry);"
            " window.fetch = async (u, o) => { const url = String((u && u.url) || u);"
            "  const hit = (window.__dsh_mocks || []).find((m) => url.indexOf(m.needle) >= 0"
            "    && (!m.limit || m.hits < m.limit));"
            "  if (hit) { hit.hits += 1; return new Response(hit.body, { status: hit.status,"
            "    headers: { 'Content-Type': 'application/json' } }); }"
            "  return orig(u, o); }; })();"
        )
        lines.append(f"await page.cdp('Page.addScriptToEvaluateOnNewDocument', {{ source: {_js(source)} }});")
        step_ok("mock_route")
    elif verb == "mock_stream":
        # 用法: mock_stream <url包含串> <SSE文本>
        # 把指定接口替换成一段 SSE 流式响应，用于验证流式渲染。
        needle = arg1
        stream = arg2 or "data: ok\\n\\ndata: [DONE]\\n\\n"
        source = (
            "(() => { const orig = window.fetch; window.__dsh_streams = window.__dsh_streams || [];"
            f" window.__dsh_streams.push({{needle: {_js(needle)}, body: {_js(stream)}}});"
            " window.fetch = async (u, o) => { const url = String((u && u.url) || u);"
            "  const hit = (window.__dsh_streams || []).find((m) => url.indexOf(m.needle) >= 0);"
            "  if (hit) { const enc = new TextEncoder();"
            "    const rs = new ReadableStream({ start(c) { c.enqueue(enc.encode(hit.body)); c.close(); } });"
            "    return new Response(rs, { status: 200,"
            "      headers: { 'Content-Type': 'text/event-stream' } }); }"
            "  return orig(u, o); }; })();"
        )
        lines.append(f"await page.cdp('Page.addScriptToEvaluateOnNewDocument', {{ source: {_js(source)} }});")
        step_ok("mock_stream")
    elif verb == "wait_function":
        expression = " ".join(p for p in (arg1, arg2) if p)
        lines.append(f"await page.waitForFunction({_js(expression)}, null, {{ timeout: {timeout_ms} }});")
        step_ok("wait_function")
    elif verb == "upload":
        # 用法: upload <selector> <绝对路径>; 多路径用 | 分隔
        if not arg1 or not arg2:
            raise ValueError("upload 动作需提供 selector 与绝对路径")
        paths = [str(Path(p.strip()).resolve()) for p in arg2.split("|") if p.strip()]
        lines.append(f"await page.setInputFiles({_js(arg1)}, {_js(paths)});")
        step_ok("upload")
    elif verb == "download":
        # 用法: download <触发selector> <保存绝对路径>
        if not arg1 or not arg2:
            raise ValueError("download 动作需提供触发 selector 与保存路径")
        dest = str(Path(arg2).resolve())
        lines.append(f"const dp{idx} = page.waitForEvent('download', {{ timeout: {timeout_ms} }});")
        lines.append(f"await page.click({_js(arg1)});")
        lines.append(f"const dl{idx} = await dp{idx};")
        lines.append(f"await dl{idx}.saveAs({_js(dest)});")
        report_extra = f"report.downloads.push({{ url: dl{idx}.url(), path: {_js(dest)} }});"
        lines.append(report_extra)
        step_ok("download")
    elif verb == "popup":
        # 用法: popup <触发selector>；新页面作为 p2/task.page
        if not arg1:
            raise ValueError("popup 动作需提供触发 selector")
        lines.append(f"const pp{idx} = page.waitForEvent('popup');")
        lines.append(f"await page.click({_js(arg1)});")
        lines.append(f"const pop{idx} = await pp{idx};")
        lines.append(f"await pop{idx}.waitForLoadState();")
        lines.append(f"report.skill_results.push({{ skill: 'popup', args: {{ selector: {_js(arg1)} }}, "
                     f"result: await pop{idx}.url() }});")
        step_ok("popup")
    elif verb in ("accept_dialog", "dismiss_dialog"):
        method = "acceptDialog" if verb == "accept_dialog" else "dismissDialog"
        lines.append(f"await page.{method}({_js(arg1)});" if arg1 else f"await page.{method}();")
        step_ok(verb)
    elif verb in ("new_page", "adopt", "release", "user_page", "pages", "tabs"):
        if verb == "new_page":
            lines.append("const np = await task.newPage();")
            lines.append("report.skill_results.push({ skill: 'new_page', args: {}, result: np.label });")
        elif verb == "adopt":
            lines.append(f"const ad = await task.adopt((await task.tabs())"
                         f".find((t) => t.active && !t.label)?.page);")
            lines.append("report.skill_results.push({ skill: 'adopt', args: {}, result: ad?.label || 'none' });")
        elif verb == "release":
            lines.append(f"await task.release({_js(arg1 or 'p1')});")
            lines.append(f"report.skill_results.push({{ skill: 'release', args: {{ label: {_js(arg1 or 'p1')} }}, result: 'ok' }});")
        elif verb == "user_page":
            lines.append("const up = task.userPage();")
            lines.append("report.skill_results.push({ skill: 'user_page', args: {}, result: await up.url() });")
        else:
            method = "pages" if verb == "pages" else "tabs"
            lines.append(f"const listing = await task.{method}();")
            lines.append(f"report.skill_results.push({{ skill: {_js(verb)}, args: {{}}, "
                         f"result: listing.map((p) => p.label || p.targetId) }});")
        step_ok(verb)
    elif verb == "skill":
        if len(parts) < 2 or not arg1:
            raise ValueError("skill 动作缺少技能名")
        if whitelist and arg1 not in whitelist:
            raise ValueError(f"技能 `{arg1}` 不在 ego_browser.skill_whitelist 中")
        try:
            args = json.loads(arg2) if arg2.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(f"skill 参数不是合法 JSON: {e}")
        lines.extend(_emit_skill(arg1, args, idx, shot_dir, cid, timeout_ms))
        step_ok("skill:" + arg1)
    else:
        raise ValueError(f"未知 UI 动作 `{verb}`（步骤: {action}）")
    return lines


def _emit_skill(skill: str, args: dict, idx: int, shot_dir: Path, cid: str,
                timeout_ms: int = 30000) -> list:
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
        script = args.get("script") or args.get("expression") or "null"
        has_arg = "arg" in args and args.get("arg") is not None
        stripped = script.strip()
        is_function = stripped.startswith("function") or stripped.startswith("async ") or "=>" in stripped
        if has_arg:
            fn = script if is_function else f"(__arg) => ({script})"
            lines.append(f"const r{idx} = await page.evaluate({_js(fn)}, a{idx}.arg);")
        else:
            lines.append(f"const r{idx} = await page.evaluate({_js(script)});")
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
    elif skill == "mouse":
        method = str(args.get("action") or "click")
        x, y = int(args.get("x") or 0), int(args.get("y") or 0)
        lines.append(f"await page.mouse.{method}({x}, {y}, {{ label: {_js(args.get('label') or 'dsh mouse')} }});")
        lines.append(f"report.skill_results.push({{ skill: 'mouse', args: a{idx}, result: 'ok' }});")
    elif skill == "keyboard":
        method = str(args.get("action") or "press")
        value = str(args.get("text") or args.get("key") or "")
        if method in ("paste", "insertText"):
            lines.append(f"await page.keyboard.{method}({_js(value)});")
        else:
            lines.append(f"await page.keyboard.{method}({_js(value)});")
        lines.append(f"report.skill_results.push({{ skill: 'keyboard', args: a{idx}, result: 'ok' }});")
    elif skill == "popup":
        lines.append(f"const pp{idx} = page.waitForEvent('popup');")
        lines.append(f"await page.click({_js(args.get('selector') or 'a')});")
        lines.append(f"const pop{idx} = await pp{idx}; await pop{idx}.waitForLoadState();")
        lines.append(f"report.skill_results.push({{ skill: 'popup', args: a{idx}, "
                     f"result: await pop{idx}.url() }});")
    elif skill == "upload":
        paths = [str(Path(p).resolve()) for p in (args.get("paths") or [])]
        lines.append(f"await page.setInputFiles({_js(args.get('selector') or 'input[type=file]')}, {_js(paths)});")
        lines.append(f"report.skill_results.push({{ skill: 'upload', args: a{idx}, result: 'ok' }});")
    elif skill == "download":
        dest = str(Path(args.get("path") or (shot_dir / f"download-{cid}")).resolve())
        lines.append(f"const dp{idx} = page.waitForEvent('download', {{ timeout: {timeout_ms} }});")
        lines.append(f"await page.click({_js(args.get('selector') or 'button.download')});")
        lines.append(f"const dl{idx} = await dp{idx}; await dl{idx}.saveAs({_js(dest)});")
        lines.append(f"report.downloads.push({{ url: dl{idx}.url(), path: {_js(dest)} }});")
        lines.append(f"report.skill_results.push({{ skill: 'download', args: a{idx}, result: {_js(dest)} }});")
    elif skill in ("adopt", "release", "user_page", "new_page", "pages", "tabs"):
        if skill == "adopt":
            lines.append("const ad = await task.adopt((await task.tabs())"
                         ".find((t) => t.active && !t.label)?.page);")
            result_expr = "ad?.label || 'none'"
        elif skill == "release":
            lines.append(f"await task.release({_js(args.get('label') or 'p1')});")
            result_expr = "'ok'"
        elif skill == "user_page":
            lines.append("const up = task.userPage();")
            result_expr = "await up.url()"
        elif skill == "new_page":
            lines.append("const np = await task.newPage();")
            result_expr = "np.label"
        else:
            method = "pages" if skill == "pages" else "tabs"
            lines.append(f"const listing = await task.{method}();")
            result_expr = "listing.map((p) => p.label || p.targetId)"
        lines.append(f"report.skill_results.push({{ skill: {_js(skill)}, args: a{idx}, "
                     f"result: {result_expr} }});")
    elif skill == "wait_function":
        lines.append(f"await page.waitForFunction({_js(args.get('expression') or 'true')}, null, "
                     f"{{ timeout: {args.get('timeout') or timeout_ms} }});")
        lines.append(f"report.skill_results.push({{ skill: 'wait_function', args: a{idx}, result: 'ok' }});")
    elif skill == "reload":
        lines.append("await page.reload(); await page.waitForLoadState();")
        lines.append(f"report.skill_results.push({{ skill: 'reload', args: a{idx}, result: 'ok' }});")
    elif skill in ("clear_state", "clear_cookies"):
        lines.append("await page.cdp('Storage.clearDataForOrigin', { origin: a" + str(idx)
                     + ".origin || location.origin, storageTypes: 'cookies,local_storage,indexed_db' });")
        if args.get("reload", True):
            lines.append("await page.reload({ ignoreCache: true });")
        lines.append(f"report.skill_results.push({{ skill: {_js(skill)}, args: a{idx}, result: 'ok' }});")
    elif skill == "claim_space":
        lines.append(f"const cs = await claimTaskSpace({_js(args.get('spaceId') or '')});")
        lines.append(f"report.skill_results.push({{ skill: 'claim_space', args: a{idx}, result: cs.spaceId }});")
    else:
        raise ValueError(f"暂不支持的 EGO 技能 `{skill}`，请查看运行时指南: {guide_path()}")
    return lines


def run_case(case_id: str, steps: list, base_url: str = "", run_id: str = "",
             screenshot_dir: str | None = None) -> dict:
    """用 ego-browser CLI 执行一条 UI 用例，返回 runtime 明细。"""
    guide = get_guide()
    ego = _ego_cfg()
    whitelist = set(ego.get("skill_whitelist") or [])
    shot_dir = Path(screenshot_dir) if screenshot_dir else temp_root() / "screenshots"
    shot_dir = shot_dir.resolve()
    shot_dir.mkdir(parents=True, exist_ok=True)
    timeout_ms = assert_timeout_ms()

    configured_space = os.environ.get("DSH_EGO_TASK_SPACE_ID") or ego.get("task_space_id") or ""
    prefix = str(ego.get("task_space_name_prefix") or "dsh")
    space_name = f"{prefix}-{safe_case_id(run_id or 'run')}-{safe_case_id(case_id)}"
    space_literal = _js(int(configured_space)) if str(configured_space).strip().isdigit() else "null"
    js_lines = [
        f"const SHOT_DIR = {_js(str(shot_dir))};",
        "const report = { status: 'passed', steps: [], assert_detail: [], skill_results: [], "
        "downloads: [], screenshot_path: '' };",
        "let currentStep = 0;",
        f"const SPACE_ID = {space_literal};",
        f"const SPACE_NAME = {_js(space_name)};",
        "const entry = SPACE_ID === null ? undefined : (await listTaskSpaces()).find((s) => s.id === SPACE_ID);",
        "const task = !entry ? await taskSpace(SPACE_NAME)",
        "  : entry.ownership === 'user' ? await claimTaskSpace(SPACE_ID)",
        "  : entry.ownership === 'agentDelegatedToUser' ? await takeOverTaskSpace(SPACE_ID)",
        "  : await taskSpace(SPACE_ID);",
        "const page = (await task.pages())[0] ?? (await task.adopt((await task.tabs())[0].page));",
        f"const PAGE_SESSION = {_js(_page_session_source())};",
        "try {",
    ]
    has_mock = any(
        str((st.get("action") if isinstance(st, dict) else st) or "").startswith(("mock_route ", "mock_stream "))
        for st in (steps or [])
    )
    bootstrap_done = False
    for idx, step in enumerate(steps or [], 1):
        action = step.get("action", "") if isinstance(step, dict) else str(step)
        js_lines.extend(_emit_action(str(action), idx, base_url, shot_dir, case_id, whitelist,
                                     timeout_ms, has_mock=has_mock))
        # 会话自举要在首次导航之后做：导航前页面还是 about:blank，判断没有意义。
        if not bootstrap_done and str(action).startswith("goto "):
            js_lines.extend(["  " + ln for ln in SESSION_BOOTSTRAP_JS])
            bootstrap_done = True
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
    # 复用给定的已登录空间时必须保活：SSO 会话只存在于既有空间里，
    # 新建空间会被 SSO 网关重定向到登录页（已实测），一旦 finish 掉后续用例全部失效。
    reuse = bool(str(configured_space).strip().isdigit())
    if space_id is not None and not reuse:
        try:
            _run_js(f"const task = await taskSpace({_js(space_id)}); await task.finish({{ keep: [] }}); "
                    "console.log(JSON.stringify({ finished: true }));")
        except Exception as e:
            result["finish_error"] = str(e)[:500]
    elif reuse:
        result["space_reused"] = True
    return result
