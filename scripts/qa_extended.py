#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行 qa-toolkit 扩展能力（登录 / 真实 API 执行 / 模板规范 / 产物审核）。

结果写入 records/<run_id>/qa-extended.json，供报告与追溯使用。
配置见 config/providers.yaml → qa_casepipe.extended。

用法:
  python3 scripts/qa_extended.py [--run run-xxx] [--dry-run]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, ensure_dirs, init_run_id, load_providers, read_json,
    resolve_run_dir, write_json,
)
import qa_casepipe  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="qa-toolkit 扩展能力执行器")
    parser.add_argument("--run", help="批次 run_id（默认取当前批次）")
    parser.add_argument("--dry-run", action="store_true", help="只打印配置，不执行")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    run_id = args.run or init_run_id()
    run_dir = resolve_run_dir(Path(paths["records"]), run_id)
    providers = load_providers()
    pipe = qa_casepipe.casepipe_config(providers)
    ext = ((providers or {}).get("qa_casepipe") or {}).get("extended") or {}

    if args.dry_run:
        print(f"qa-toolkit 扩展能力（dry-run）: enabled={ext.get('enabled')} "
              f"run_id={run_id} scripts={qa_casepipe.find_scripts_dir(pipe['scripts_dir'])}")
        for name, item in (ext.items() if isinstance(ext, dict) else []):
            if isinstance(item, dict):
                print(f"  {name}: enabled={item.get('enabled')}")
        return

    result = qa_casepipe.extended_run(run_dir, pipe, providers)
    write_json(run_dir / "qa-extended.json", result)
    if not result.get("enabled"):
        print("qa-toolkit 扩展能力: 未启用（providers.yaml → qa_casepipe.extended.enabled）")
        return
    steps = result.get("steps") or []
    print(f"qa-toolkit 扩展能力: {len(steps)} 项 | "
          f"{'全部成功' if result.get('ok') else '存在失败，详见 qa-extended.json'}")
    for step in steps:
        flag = "OK" if step.get("ok") else "FAIL"
        print(f"  [{flag}] {step.get('name')}: {step.get('error') or step.get('out') or ''}")


if __name__ == "__main__":
    main()
