#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接入 ego-browser 的 learnings 领域工具包（GitHub / Google / X 等）。

工具包以 manifest.json + Node 工具（ESM 导出）+ 浏览器内工具（纯函数）+ 选择器笔记
组成。本模块负责发现、列举、执行这些工具，执行仍走 `ego-browser nodejs`，
不复制第三方工具代码。

用法:
  python3 scripts/ego_learnings.py list
  python3 scripts/ego_learnings.py list --domain github
  python3 scripts/ego_learnings.py run --domain github --tool search_repos \
      --args '{"query":"dsh-workflow"}'
  python3 scripts/ego_learnings.py snippet --domain github --tool repo-stats
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import WORKFLOW_ROOT, load_providers  # noqa: E402
from ego_client import cli_path, timeout_sec  # noqa: E402


def find_learnings_dir() -> Path | None:
    """按优先级探测 ego-browser learnings 目录。"""
    candidates = []
    env = os.environ.get("DSH_EGO_LEARNINGS_DIR")
    if env:
        candidates.append(Path(env))
    ego_cfg = (load_providers().get("ego_browser") or {})
    if ego_cfg.get("learnings_dir"):
        candidates.append(Path(ego_cfg["learnings_dir"]))
    candidates += [
        WORKFLOW_ROOT.parent / "ego-browser" / "learnings",
        WORKFLOW_ROOT / "node_modules" / "ego-browser" / "learnings",
        Path.home() / ".local" / "share" / "ego-browser" / "learnings",
    ]
    profiles = Path.home() / ".dsh" / "profiles"
    if profiles.exists():
        for profile in profiles.iterdir():
            candidates.append(profile / "node_modules" / "ego-browser" / "learnings")
    for path in candidates:
        if path and path.is_dir():
            return path.resolve()
    return None


def _manifests(root: Path) -> list:
    rows = []
    for path in sorted(root.glob("*/manifest.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        data["_dir"] = str(path.parent)
        rows.append(data)
    return rows


def list_tools(domain: str = "") -> dict:
    root = find_learnings_dir()
    if not root:
        return {"ok": False, "error": "未找到 ego-browser learnings 目录"}
    items = []
    for manifest in _manifests(root):
        if domain and manifest.get("id") != domain:
            continue
        for kind in ("nodeTools", "browserTools"):
            for name, spec in (manifest.get(kind) or {}).items():
                items.append({
                    "domain": manifest.get("id"), "tool": name, "kind": kind,
                    "description": spec.get("description") or "",
                    "callable": spec.get("callable") or "",
                    "path": str((Path(manifest["_dir"]) / spec["path"]).resolve())
                    if spec.get("path") else "",
                })
        for note in manifest.get("notes") or []:
            np = Path(manifest["_dir"]) / note
            if np.exists():
                items.append({"domain": manifest.get("id"), "tool": note,
                              "kind": "notes", "description": "", "path": str(np.resolve())})
    return {"ok": True, "root": str(root), "tools": items}


def _run_js(js: str) -> dict:
    try:
        proc = subprocess.run([cli_path(), "nodejs"], input=js, text=True,
                              capture_output=True, timeout=timeout_sec())
    except FileNotFoundError:
        return {"ok": False, "error": f"未找到 CLI: {cli_path()}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "执行超时"}
    for line in reversed((proc.stdout or "").splitlines()):
        try:
            data = json.loads(line)
            if isinstance(data, dict) and "error" not in data:
                return {"ok": proc.returncode == 0, "result": data}
            return {"ok": False, "error": data}
        except json.JSONDecodeError:
            continue
    return {"ok": False, "error": (proc.stderr or proc.stdout or "")[-1000:]}


def run_tool(domain: str, tool: str, args: dict) -> dict:
    listing = list_tools(domain)
    target = next((t for t in listing.get("tools", [])
                   if t["domain"] == domain and t["tool"] == tool), None)
    if not target:
        return {"ok": False, "error": f"未找到工具 {domain}/{tool}",
                "available": [t["tool"] for t in listing.get("tools", [])
                              if t["domain"] == domain]}
    if target["kind"] != "nodeTools":
        return {"ok": False, "error": f"工具 {domain}/{tool} 是 {target['kind']}，"
                                      f"请用 snippet 模式生成注入片段"}
    js = (
        f"import {{ {target['callable']} }} from {json.dumps(target['path'])};\n"
        f"const result = await {target['callable']}(ctx, {json.dumps(args, ensure_ascii=False)});\n"
        "console.log(JSON.stringify(result));\n"
    )
    result = _run_js(js)
    result.update({"domain": domain, "tool": tool, "path": target["path"]})
    return result


def snippet(domain: str, tool: str) -> dict:
    listing = list_tools(domain)
    target = next((t for t in listing.get("tools", [])
                   if t["domain"] == domain and t["tool"] == tool), None)
    if not target:
        return {"ok": False, "error": f"未找到工具 {domain}/{tool}"}
    if target["kind"] == "browserTools":
        code = Path(target["path"]).read_text(encoding="utf-8")
        return {"ok": True, "domain": domain, "tool": tool, "kind": "browserTools",
                "snippet": f"await page.evaluate(\n{code.rstrip()},\n  ARGS\n);"}
    return {"ok": False, "error": f"工具 {domain}/{tool} 不是浏览器内工具"}


def main():
    parser = argparse.ArgumentParser(description="ego-browser learnings 领域工具包")
    sub = parser.add_subparsers(dest="action", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--domain")
    p_run = sub.add_parser("run")
    p_run.add_argument("--domain", required=True)
    p_run.add_argument("--tool", required=True)
    p_run.add_argument("--args", default="{}")
    p_snip = sub.add_parser("snippet")
    p_snip.add_argument("--domain", required=True)
    p_snip.add_argument("--tool", required=True)
    args = parser.parse_args()

    if args.action == "list":
        print(json.dumps(list_tools(args.domain), ensure_ascii=False, indent=2))
    elif args.action == "run":
        try:
            payload = json.loads(args.args)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--args 不是合法 JSON: {exc}")
        print(json.dumps(run_tool(args.domain, args.tool, payload), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(snippet(args.domain, args.tool), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
