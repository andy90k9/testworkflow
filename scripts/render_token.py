#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 API 用例渲染到运行期临时副本，仓库内文件始终保留占位符。"""
import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import load_config, resolve, records_root  # noqa: E402

PLACEHOLDER = "__DSH_API_TOKEN__"


def _token_from_file(token_file: str) -> str:
    path = Path(token_file).expanduser()
    if not path.exists():
        raise SystemExit(f"token 文件不存在: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        print(f"[render-token] 警告: token 文件权限过宽（{oct(mode)}），建议 600", file=sys.stderr)
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"token 文件为空: {path}")
    return token


def render_api_case(cfg: dict, run_id: str, token_file: str, source: str = "", output: str = "") -> Path:
    acfg = ((cfg.get("_providers") or {}).get("spec_adapter") or {})
    # spec-adapter 是独立配置，不在 providers 中；这里直接读取 workflow.generation 指向的文件。
    generation = cfg.get("generation") or {}
    adapter_path = resolve(str(generation.get("spec_adapter_file") or "config/spec-adapter.yaml"))
    if adapter_path.exists():
        import yaml
        with open(adapter_path, "r", encoding="utf-8") as f:
            acfg = (yaml.safe_load(f) or {}).get("spec_adapter") or {}
    source_path = resolve(source or str((acfg.get("output") or {}).get("api") or "cases/api/api.yaml"))
    if not source_path.exists():
        raise SystemExit(f"API 用例不存在: {source_path}")

    target = resolve(output) if output else (
        records_root(cfg) / run_id / "rendered" / "cases" / "api.rendered.yaml"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    text = source_path.read_text(encoding="utf-8")
    token = _token_from_file(token_file)
    count = text.count(PLACEHOLDER)
    if count == 0:
        print("[render-token] 警告: 用例中未找到 token 占位符", file=sys.stderr)
    target.write_text(text.replace(PLACEHOLDER, token), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    print(f"[render-token] 已渲染 {count} 处到临时副本: {target}")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="渲染 API 用例 token 到临时副本")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    cfg = load_config()
    render_api_case(cfg, args.run_id, args.token_file, args.source, args.output)


if __name__ == "__main__":
    main()
