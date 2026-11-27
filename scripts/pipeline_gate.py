#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流水线硬门禁：校验转换报告、scope-manifest 与用例文件一致性。

失败统一以退出码 2 中止，避免空范围或错范围继续生成报告。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, resolve, read_json, canonical_hash, WORKFLOW_ROOT,
)

LAYERS = ("functional", "api", "ui")


def _fail(problems: list, message: str) -> None:
    problems.append(message)


def main() -> None:
    parser = argparse.ArgumentParser(description="DSH 流水线硬门禁")
    parser.add_argument("--manifest", default=os.environ.get("DSH_SCOPE_MANIFEST", ""))
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    cfg = load_config()
    gate = ((cfg.get("pipeline") or {}).get("gate") or {})
    selection = ((cfg.get("cases") or {}).get("selection") or {})
    report_path = resolve(args.report or selection.get("spec_report")
                          or "reports/spec-conversion-report.json")
    manifest_path = resolve(args.manifest) if args.manifest else None

    problems = []

    if not report_path.exists():
        _fail(problems, f"转换报告不存在: {report_path}")
        report = {}
    else:
        report = read_json(report_path) or {}

    if gate.get("fail_on_spec_hash_missing", True) and not report.get("spec_hash"):
        _fail(problems, "转换报告缺少 spec_hash")

    id_check = report.get("id_check") or {}
    if gate.get("fail_on_id_check", True) and not id_check.get("ok", False):
        _fail(problems, f"ID 完整性校验失败: {id_check}")

    if manifest_path is None:
        _fail(problems, "未指定 scope-manifest（DSH_SCOPE_MANIFEST）")
        manifest = {}
    elif not manifest_path.exists():
        _fail(problems, f"scope-manifest 不存在: {manifest_path}")
        manifest = {}
    else:
        manifest = read_json(manifest_path) or {}

    if manifest:
        if str(manifest.get("schema_version") or "") != "1.0":
            _fail(problems, f"manifest schema_version 不兼容: {manifest.get('schema_version')}")
        if str(manifest.get("spec", {}).get("hash") or "") != str(report.get("spec_hash") or ""):
            _fail(problems, "manifest.spec.hash 与转换报告 spec_hash 不一致")

        expected_hash = canonical_hash(
            {k: v for k, v in manifest.items() if k != "manifest_hash"}
        )
        if gate.get("fail_on_manifest_mismatch", True) and expected_hash != manifest.get("manifest_hash"):
            _fail(problems, "manifest_hash 无法复算，清单可能被修改")

        counts = report.get("counts") or {}
        for layer in LAYERS:
            ids = manifest.get("case_ids", {}).get(layer) or []
            files = manifest.get("case_files", {}).get(layer) or []
            if gate.get("fail_on_empty_cases", True) and not ids:
                _fail(problems, f"{layer} 用例为空")
            if gate.get("fail_on_count_mismatch", True) and counts.get(layer) is not None:
                if int(counts.get(layer) or 0) != len(ids):
                    _fail(problems, f"{layer} 数量不一致: report={counts.get(layer)} manifest={len(ids)}")
            for rel in files:
                path = resolve(str(rel))
                if not path.exists():
                    _fail(problems, f"{layer} 用例文件不存在: {rel}")

        env = manifest.get("env") or {}
        if gate.get("fail_on_missing_env", True):
            for key in ("api_base_url", "ui_base_url"):
                if not str(env.get(key) or "").strip():
                    _fail(problems, f"manifest.env.{key} 为空")

    if problems:
        print("硬门禁未通过：")
        for item in problems:
            print(f"  - {item}")
        raise SystemExit(2)

    print(
        "硬门禁通过: "
        + " / ".join(
            f"{layer}={len(manifest.get('case_ids', {}).get(layer) or [])}"
            for layer in LAYERS
        )
        + f" | manifest={manifest_path}"
    )


if __name__ == "__main__":
    main()
