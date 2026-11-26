#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 cases/ui/*.yaml 的 DSL 生成 Playwright 定位脚本（*.e2e.ts）。

与 tests/ui/test_ui_cases.py 共用同一份 DSL 用例源，生成的脚本纳入批次产物
records/<run_id>/playwright/，并输出 manifest.json 供报告与 MySQL 回流使用。

用法:
  python3 scripts/gen_playwright.py                    # 当前批次生成
  python3 scripts/gen_playwright.py --run run-xxx      # 指定批次
  python3 scripts/gen_playwright.py --import <dir>     # 导入已有生成的脚本目录
  python3 scripts/gen_playwright.py --dry-run          # 只预览，不写盘
"""
import argparse
import hashlib
import json
import re
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, ensure_dirs, init_run_id, read_json, write_json, resolve_run_dir,
)

GENERATOR = "dsh-workflow/gen_playwright v1.0"
MANIFEST_NAME = "playwright-manifest.json"


def _ts(value) -> str:
    """转成 TypeScript 单引号字符串字面量。"""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def _feature(case_id: str) -> str:
    """UI-NAV-001 -> UI-NAV。"""
    parts = str(case_id).split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else "UI-MISC"


def _file_name(feature: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", feature.lower()).strip("-")
    return f"{safe or 'ui-misc'}.e2e.ts"


def _step_lines(action: str, base_url: str, timeout_ms: int) -> list:
    """把一条 DSL action 翻译成 Playwright/TS 代码行。

    与 tests/ui/test_ui_cases.py::_run_step 的动作集保持一致；
    无法翻译的动作显式留注释，绝不静默跳过。
    """
    parts = str(action).strip().split(" ", 2)
    verb = (parts[0] if parts else "").lower()
    arg1 = parts[1] if len(parts) > 1 else ""
    arg2 = parts[2] if len(parts) > 2 else ""
    t = f"{{ timeout: {timeout_ms} }}"
    if verb == "goto":
        path = arg1 or "/"
        if path.startswith(("http://", "https://")):
            return [f"await page.goto({_ts(path)}, {{ waitUntil: 'domcontentloaded' }});"]
        return [f"await page.goto(base + {_ts('/' + path.lstrip('/'))}, "
                f"{{ waitUntil: 'domcontentloaded' }});"]
    if verb == "wait":
        try:
            ms = int(float(arg1 or 1) * 1000)
        except ValueError:
            ms = 1000
        return [f"await page.waitForTimeout({ms});"]
    if verb == "click":
        return [f"await page.click({_ts(arg1)}, {t});"]
    if verb == "fill":
        return [f"await page.fill({_ts(arg1)}, {_ts(arg2)}, {t});"]
    if verb == "press":
        return [f"await page.press({_ts(arg1)}, {_ts(arg2)}, {t});"]
    if verb == "check":
        return [f"await page.check({_ts(arg1)}, {t});"]
    if verb == "select":
        return [f"await page.selectOption({_ts(arg1)}, {_ts(arg2)}, {t});"]
    if verb == "hover":
        return [f"await page.hover({_ts(arg1)}, {t});"]
    if verb == "expect_url_contains":
        return [f"expect(page.url().replace(/\\?.*$/, '')).toContain({_ts(arg1)});"]
    if verb == "expect_text":
        return [f"await expect(page.locator({_ts(arg1)})).toContainText({_ts(arg2)}, {t});"]
    if verb == "expect_visible":
        return [f"await expect(page.locator({_ts(arg1)})).toBeVisible({t});"]
    if verb == "screenshot":
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", arg1 or "shot")
        return [f"await page.screenshot({{ path: 'records/' + runId + "
                f"'/playwright/screenshots/{name}.png', fullPage: true }});"]
    if verb == "skill":
        return [f"// 注意：skill 动作需 EGO Skill，Playwright 原生不支持（原步骤: {action}）"]
    return [f"// 未识别动作，未执行（原步骤: {action}）"]


def _test_block(case: dict, base_url: str, timeout_ms: int, auth: dict) -> str:
    case_id = case.get("case_id") or "UI-UNKNOWN"
    title = str(case.get("title") or case_id).replace("'", "\\'")
    lines = [
        f"  test({_ts(case_id + ': ' + title)}, async ({{ page }}) => {{",
        f"    const runId = process.env.DSH_RUN_ID || '';",
        f"    const base = process.env.E2E_BASE_URL || {_ts(base_url)};",
    ]
    if auth.get("enabled"):
        lines.extend([
            "    await page.context().addInitScript(() => {",
            "      try {",
        ])
        for key, value in (auth.get("storage") or {}).items():
            lines.append(f"        sessionStorage.setItem({_ts(key)}, {_ts(value)});")
        lines.extend(["      } catch {}", "    });"])
    for step in case.get("steps") or []:
        action = step.get("action", "") if isinstance(step, dict) else str(step)
        if not action:
            continue
        lines.append(f"    // step: {action}")
        lines.extend("    " + line for line in _step_lines(action, base_url, timeout_ms))
    lines.append("  });")
    return "\n".join(lines)


def _file_header(feature: str, generated_at: str, source: str) -> str:
    return "\n".join([
        "/**",
        f" * Auto-generated Playwright E2E tests for: {feature}",
        f" * Generated at: {generated_at}",
        f" * Source: {source}",
        f" * @generated by {GENERATOR}",
        " */",
    ])


def _config_source(base_url: str, timeout_ms: int) -> str:
    return "\n".join([
        "/**",
        " * Auto-generated Playwright config (dsh-workflow).",
        f" * @generated by {GENERATOR}",
        " */",
        "import { defineConfig } from '@playwright/test';",
        "",
        "export default defineConfig({",
        "  testMatch: /.*\\.e2e\\.ts$/,",
        f"  timeout: {timeout_ms},",
        "  reporter: [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],",
        "  outputDir: 'test-results',",
        "  use: {",
        f"    baseURL: {_ts(base_url)},",
        "    headless: true,",
        "    screenshot: 'only-on-failure',",
        "    video: 'off',",
        "    trace: 'off',",
        "  },",
        "});",
    ])


def _load_ui_cases(paths: dict) -> list:
    cases = []
    for path in sorted(Path(paths["ui_cases"]).glob("*.yaml")):
        if path.name.startswith("example."):
            continue
        data = read_json(path) if path.suffix.lower() == ".json" else None
        if data is None:
            try:
                import yaml
            except ImportError:
                print("PyYAML 未安装，无法解析 UI 用例 YAML", file=sys.stderr)
                return cases
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or not item.get("case_id"):
                continue
            cases.append(item)
    return cases


def _write_outputs(files: dict, out_dir: Path, dry_run: bool) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, content in files.items():
        path = out_dir / name
        sha = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
        if not dry_run:
            path.write_text(content, encoding="utf-8")
        rows.append({"name": name, "path": str(path), "sha1": sha,
                     "lines": len(content.splitlines())})
    return rows


def generate(paths: dict, run_id: str, dry_run: bool = False) -> dict:
    cfg = load_config()
    if not (cfg.get("playwright") or {}).get("enabled", True):
        return {"run_id": run_id, "generator": GENERATOR, "mode": "disabled",
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "features": [], "files": [], "total_cases": 0}
    tests = cfg.get("tests") or {}
    base_url = str(tests.get("ui_base_url") or "http://127.0.0.1:5173")
    timeout_ms = int(tests.get("ui_timeout_sec") or 20) * 1000
    auth = {
        "enabled": bool((cfg.get("playwright") or {}).get("inject_auth", True)),
        "storage": (cfg.get("playwright") or {}).get("storage") or {
            "access_token": "e2e-playwright-token",
            "user_name": "e2e",
            "user_id": "1",
        },
    }
    cases = _load_ui_cases(paths)
    groups = OrderedDict()
    for case in cases:
        groups.setdefault(_feature(case.get("case_id")), []).append(case)

    generated_at = datetime.now().isoformat(timespec="seconds")
    source = "cases/ui/*.yaml (dsh-workflow DSL)"
    out_dir = resolve_run_dir(Path(paths["records"]), run_id) / "playwright"
    files = {}
    feature_rows = []
    for feature, group in groups.items():
        name = _file_name(feature)
        body = "\n\n".join(_test_block(c, base_url, timeout_ms, auth) for c in group)
        content = _file_header(feature, generated_at, source) + "\n\n" + \
            "import { test, expect } from '@playwright/test';\n\n" + \
            "test.describe.configure({ timeout: " + str(timeout_ms) + " });\n" + \
            "test.use({ screenshot: 'only-on-failure', trace: 'off' });\n\n" + \
            f"test.describe({_ts(feature)}, () => {{\n{body}\n}});\n"
        files[name] = content
        feature_rows.append({"feature": feature, "file": name,
                             "case_ids": [c.get("case_id") for c in group]})
    if files:
        files["index.ts"] = "\n".join(
            ["/**", " * Auto-generated Playwright test index (dsh-workflow).",
             f" * @generated by {GENERATOR}", " */", ""]
            + [f"import './{Path(f['file']).stem}';" for f in feature_rows]) + "\n"
        files["playwright.config.ts"] = _config_source(base_url, timeout_ms) + "\n"
    rows = _write_outputs(files, out_dir, dry_run)
    by_name = {r["name"]: r for r in rows}
    manifest = {
        "run_id": run_id,
        "generator": GENERATOR,
        "source": source,
        "generated_at": generated_at,
        "base_url": base_url,
        "mode": "generated",
        "features": [{**f, "sha1": by_name.get(f["file"], {}).get("sha1")} for f in feature_rows],
        "files": rows,
        "total_cases": len(cases),
    }
    if not dry_run:
        write_json(out_dir / MANIFEST_NAME, manifest)
    return manifest


def import_existing(paths: dict, run_id: str, src_dir: Path, dry_run: bool = False) -> dict:
    """导入已有生成器（如 e2e_gen_playwright）产出的脚本到本批次。"""
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        raise SystemExit(f"脚本目录不存在: {src_dir}")
    out_dir = resolve_run_dir(Path(paths["records"]), run_id) / "playwright"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    features = []
    for path in sorted(src_dir.rglob("*.e2e.ts")):
        content = path.read_text(encoding="utf-8")
        rel = path.relative_to(src_dir)
        dest = out_dir / rel
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        m = re.search(r"Auto-generated Playwright E2E tests for:\s*(\S+)", content)
        ids = re.findall(r"test\(['\"]([^'\"]+?):", content)
        files.append({"name": str(rel), "path": str(dest),
                      "sha1": hashlib.sha1(content.encode("utf-8")).hexdigest()[:16],
                      "lines": len(content.splitlines())})
        features.append({"feature": m.group(1) if m else path.stem,
                         "file": str(rel), "case_ids": ids})
    for extra in ("index.ts", "playwright.config.ts"):
        path = src_dir / extra
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if not dry_run:
                (out_dir / extra).write_text(content, encoding="utf-8")
            files.append({"name": extra, "path": str(out_dir / extra),
                          "sha1": hashlib.sha1(content.encode("utf-8")).hexdigest()[:16],
                          "lines": len(content.splitlines())})
    manifest = {
        "run_id": run_id,
        "generator": "imported/" + GENERATOR,
        "source": str(src_dir),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "imported",
        "features": features,
        "files": files,
        "total_cases": sum(len(f["case_ids"]) for f in features),
    }
    if not dry_run:
        write_json(out_dir / MANIFEST_NAME, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description="生成 Playwright 定位脚本产物")
    parser.add_argument("--run", help="批次 run_id（默认取 records/current-run.json）")
    parser.add_argument("--import", dest="import_from", help="导入已有生成脚本目录")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写盘")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    run_id = args.run or init_run_id()

    if args.import_from:
        manifest = import_existing(paths, run_id, Path(args.import_from), args.dry_run)
        verb = "导入预览" if args.dry_run else "已导入"
    else:
        manifest = generate(paths, run_id, args.dry_run)
        verb = "生成预览" if args.dry_run else "已生成"
    print(f"Playwright 脚本{verb}: {manifest['total_cases']} 条用例 | "
          f"{len(manifest['features'])} 个脚本 | {len(manifest['files'])} 个文件 "
          f"| run_id={run_id}")
    for f in manifest["features"]:
        print(f"  {f['file']}: {len(f['case_ids'])} 用例")


if __name__ == "__main__":
    main()
