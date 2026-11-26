#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动 EGO、打开被测系统并尝试 SSO 登录。

凭证只从环境变量读取（DSH_TARGET_URL / DSH_SSO_USER / DSH_SSO_PASSWORD），
不写入文件、报告或数据库，也不打印到 stdout/stderr。
无法自动登录时调用 task.handOff()，把浏览器控制权交还用户手动登录。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import WORKFLOW_ROOT, safe_case_id  # noqa: E402
from ego_client import _run_js, _parse_result, timeout_sec  # noqa: E402

URL = os.environ.get("DSH_TARGET_URL", "").strip()
USER = os.environ.get("DSH_SSO_USER", "").strip()
PASSWORD = os.environ.get("DSH_SSO_PASSWORD", "")
AUTO_LOGIN = os.environ.get("DSH_AUTO_LOGIN", "true").lower() != "false"
RUN_ID = os.environ.get("DSH_RUN_ID") or f"prep-{int(time.time())}"


def _ensure_ego_running() -> dict:
    """确保 ego lite 已启动且 ego-browser 命令可用。"""
    info = {"cli": None, "app_launched": False, "note": ""}
    cli = shutil.which("ego-browser")
    if not cli:
        if sys.platform == "darwin":
            subprocess.run(["open", "-a", "ego lite"], capture_output=True, text=True, timeout=30)
            info["app_launched"] = True
            for _ in range(20):
                time.sleep(1)
                cli = shutil.which("ego-browser")
                if cli:
                    break
        elif sys.platform == "win32":
            app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ego lite" / "ego lite.exe"
            if app.exists():
                subprocess.Popen([str(app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                info["app_launched"] = True
    info["cli"] = cli
    if not cli:
        info["note"] = "未找到 ego-browser 命令；请启动 ego lite 并完成 onboarding"
    return info


def _js_string(value: str) -> str:
    return json.dumps(value or "", ensure_ascii=False)


def _build_js(url: str, user: str, password: str, shot_path: Path, auto_login: bool) -> str:
    lines = [
        f"const URL = {_js_string(url)};",
        f"const USER = {_js_string(user)};",
        f"const PASSWORD = {_js_string(password)};",
        f"const SHOT = {_js_string(str(shot_path.resolve()))};",
        f"const task = await taskSpace({_js_string('dsh-target-prep-' + safe_case_id(RUN_ID))});",
        "const page = task.page('p1');",
        "let autoLogin = 'skipped';",
        "let note = '';",
        "let inputs = [];",
        "try {",
        "  await page.goto(URL);",
        "  await page.waitForLoadState();",
        "  await page.waitForTimeout(800);",
        "  inputs = await page.evaluate(() => Array.from(document.querySelectorAll('input')).map((i) => ({ type: i.type || 'text', name: i.name || '', id: i.id || '', placeholder: i.placeholder || '' })));",
    ]
    if auto_login:
        lines.extend([
            "  const passwordInput = inputs.find((i) => i.type === 'password');",
            "  if (passwordInput) {",
            "    try {",
            "      const hint = (i) => [i.type, i.name, i.id, i.placeholder].join(' ').toLowerCase();",
            "      const userInput = inputs.find((i) => /user|email|account|login|手机|账号/.test(hint(i))) || inputs.find((i) => i.type === 'text');",
            "      if (userInput && USER) {",
            "        const sel = userInput.id ? '#' + userInput.id : (userInput.name ? 'input[name=\"' + userInput.name + '\"]' : 'input[type=\"' + userInput.type + '\"]');",
            "        await page.fill(sel, USER);",
            "      }",
            "      await page.fill('input[type=\"password\"]', PASSWORD);",
            "      const buttons = ['loc=role:button[name=/登录|登陆|Login|Sign in|Sign In/i]', 'button[type=\"submit\"]', 'input[type=\"submit\"]'];",
            "      let clicked = false;",
            "      for (const sel of buttons) {",
            "        try { await page.click(sel); clicked = true; break; } catch (_) {}",
            "      }",
            "      if (!clicked) { note = '未找到登录按钮'; }",
            "      await page.waitForTimeout(3500);",
            "      const stillLogin = await page.evaluate(() => !!document.querySelector('input[type=\"password\"]'));",
            "      autoLogin = (!clicked || stillLogin) ? 'failed' : 'ok';",
            "    } catch (e) {",
            "      autoLogin = 'failed';",
            "      note = String(e && e.message ? e.message : e).slice(0, 500);",
            "    }",
            "  } else {",
            "    autoLogin = 'no_password_field';",
            "  }",
        ])
    lines.extend([
        "} catch (e) {",
        "  autoLogin = 'failed';",
        "  note = String(e && e.message ? e.message : e).slice(0, 500);",
        "}",
        "let shot = '';",
        "try { shot = await page.screenshot({ path: SHOT }); } catch (_) {}",
        "const snap = await page.snapshot({ scope: 'visible' });",
        "const manual = autoLogin !== 'ok';",
        "if (manual) { await task.handOff(); } else { await task.finish({ keep: [] }); }",
        "console.log(JSON.stringify({",
        "  url: URL, autoLogin, manualLoginRequired: manual, note,",
        "  inputCount: inputs.length, screenshotPath: shot,",
        "  snapshot: String(snap).slice(0, 800), spaceId: task.spaceId",
        "}));",
    ])
    return "\n".join(lines)


def main():
    if not URL.startswith(("http://", "https://")):
        print(json.dumps({"ok": False, "error": "被测系统链接必须以 http:// 或 https:// 开头"}, ensure_ascii=False))
        raise SystemExit(1)

    runtime = _ensure_ego_running()
    if not runtime["cli"]:
        print(json.dumps({"ok": False, "error": runtime["note"]}, ensure_ascii=False))
        raise SystemExit(1)

    shot_dir = WORKFLOW_ROOT / "records" / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    shot = shot_dir / f"target-prep-{safe_case_id(RUN_ID)}.png"

    js = _build_js(URL, USER, PASSWORD, shot, AUTO_LOGIN)
    try:
        result = _parse_result(_run_js(js))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:1000]}, ensure_ascii=False))
        raise SystemExit(1)

    ok = not result.get("manualLoginRequired", True)
    out = {
        "ok": True,
        "url": URL,
        "autoLogin": result.get("autoLogin"),
        "manualLoginRequired": bool(result.get("manualLoginRequired")),
        "note": result.get("note", ""),
        "screenshotPath": result.get("screenshotPath", ""),
        "message": ("已打开被测系统并完成自动登录" if ok else
                    "已打开被测系统，但无法自动登录；请在 EGO 浏览器中手动完成登录后重试"),
        "egoLaunched": runtime["app_launched"],
    }
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
