#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成本轮唯一用例范围清单 scope-manifest.json。

所有后续阶段通过 DSH_SCOPE_MANIFEST 读取该清单，不再各自 glob 目录。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, ensure_dirs, resolve, read_json, write_json,
    load_yaml_file, selected_case_files, scope_manifest_path,
    canonical_hash, mask_url, read_run_id, WORKFLOW_ROOT,
)

LAYERS = (
    ("functional", "functional"),
    ("api", "api_cases"),
    ("ui", "ui_cases"),
)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKFLOW_ROOT))
    except ValueError:
        return str(path)


def build_manifest(cfg: dict, paths: dict, run_id: str) -> dict:
    selection = ((cfg.get("cases") or {}).get("selection") or {})
    report_path = resolve(str(selection.get("spec_report") or "reports/spec-conversion-report.json"))
    if not report_path.exists():
        raise SystemExit(f"转换报告不存在: {report_path}")
    report = read_json(report_path) or {}
    spec_hash = str(report.get("spec_hash") or "")
    if not spec_hash:
        raise SystemExit(f"转换报告缺少 spec_hash: {report_path}")

    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generator": "scope_manifest.py",
        "spec": {
            "file": report.get("spec_file", ""),
            "hash": spec_hash,
            "version": report.get("spec_version", ""),
        },
        "case_files": {},
        "case_ids": {},
        "manual_case_ids": [],
        "runtime_binding_case_ids": [],
        "env": {
            "api_base_url": mask_url((cfg.get("tests") or {}).get("api_base_url", "")),
            "ui_base_url": mask_url((cfg.get("tests") or {}).get("ui_base_url", "")),
        },
        "source_counts": report.get("counts") or {},
    }

    manual = set()
    runtime = set()
    for layer, key in LAYERS:
        files = selected_case_files(paths[key], cfg, use_manifest=False)
        ids = []
        for path in files:
            try:
                data = load_yaml_file(path) or []
            except Exception as exc:
                raise SystemExit(f"用例解析失败: {path}: {exc}")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict) or not item.get("case_id"):
                    continue
                case_id = str(item["case_id"])
                ids.append(case_id)
                if item.get("needs_manual_context"):
                    manual.add(case_id)
                if item.get("needs_runtime_binding"):
                    runtime.add(case_id)
        manifest["case_files"][layer] = [_rel(p) for p in files]
        manifest["case_ids"][layer] = ids

    manifest["manual_case_ids"] = sorted(manual)
    manifest["runtime_binding_case_ids"] = sorted(runtime)
    manifest["manifest_hash"] = canonical_hash(
        {k: v for k, v in manifest.items() if k != "manifest_hash"}
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 scope-manifest.json")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    run_id = args.run_id or read_run_id()
    if not run_id:
        raise SystemExit("缺少 run_id，请通过 --run-id 或 DSH_RUN_ID 指定")

    manifest = build_manifest(cfg, paths, run_id)
    output = resolve(args.output) if args.output else scope_manifest_path(cfg, run_id)
    write_json(output, manifest)
    print(f"scope-manifest: {output}")
    print(
        "范围: "
        + " / ".join(f"{layer}={len(manifest['case_ids'][layer])}" for layer, _ in LAYERS)
        + f" | manual={len(manifest['manual_case_ids'])}"
        + f" | runtime={len(manifest['runtime_binding_case_ids'])}"
    )


if __name__ == "__main__":
    main()
