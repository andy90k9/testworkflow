#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 全自动一键流水线（v1.7 编排）。

阶段：
  0  run_id 初始化 + 执行锁
  1  spec 转换 + token 临时副本渲染
  2  scope-manifest 生成
  3  硬门禁
  4  格式校验
  5  执行前审计
  6  执行测试
  7  执行后审计
  8  报告
  9  MySQL 回流
  10 资产台账回流
  11 汇总退出码

退出码（位掩码，3 保留 EGO CDP 兼容语义）：
  1 测试失败 | 2 流程/门禁失败 | 4 回流失败 | 8 人工待办
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, ensure_dirs, init_run_id, load_providers,
    acquire_pipeline_lock, release_pipeline_lock, scope_manifest_path,
    records_root, read_json, write_json, resolve, WORKFLOW_ROOT,
)
from render_token import render_api_case  # noqa: E402
import qa_casepipe  # noqa: E402
import run_audit  # noqa: E402

EXIT_TEST_FAILED = 1
EXIT_PIPELINE_FAILED = 2
EXIT_BACKFLOW_FAILED = 4
EXIT_MANUAL_PENDING = 8


class StageError(RuntimeError):
    pass


def _run(stage: str, cmd: list, allow_fail: bool = False) -> int:
    print(f"\n{'=' * 62}\n▶ 阶段[{stage}]: {' '.join(cmd)}\n{'=' * 62}")
    proc = subprocess.run([sys.executable] + cmd, cwd=str(WORKFLOW_ROOT))
    code = proc.returncode
    if code != 0 and not allow_fail:
        raise StageError(f"阶段[{stage}] 失败（退出码 {code}）")
    return code


def _harness_enabled() -> bool:
    try:
        return bool((load_providers().get("harness") or {}).get("enabled"))
    except Exception:
        return False


def _lock_path(cfg: dict) -> Path:
    lock_cfg = ((cfg.get("pipeline") or {}).get("lock") or {})
    raw = str(lock_cfg.get("path") or "").strip()
    return resolve(raw) if raw else (records_root(cfg) / ".pipeline.lock")


def _summary(cfg: dict, run_id: str, test_code: int, backflow: dict, manifest: dict, only: str) -> int:
    report_path = resolve(f"reports/report-{run_id}.json")
    report = read_json(report_path) if report_path.exists() else {}
    summary = report.get("summary") or {}
    failed = int(summary.get("failed") or 0)
    errors = int(summary.get("errors") or 0)
    manual = len(manifest.get("manual_case_ids") or []) if only in ("all", "ui") else 0
    runtime_skip = len(manifest.get("runtime_binding_case_ids") or []) if only in ("all", "api") else 0
    backflow_ok = all(v in ("ok", "skipped", "dry-run") for v in backflow.values())

    release_cfg = ((cfg.get("pipeline") or {}).get("release") or {})
    block_manual = bool(release_cfg.get("block_on_manual_pending", True))
    block_runtime = bool(release_cfg.get("block_on_runtime_skip", False))
    release_ready = (
        failed == 0 and errors == 0 and backflow_ok
        and (not block_manual or manual == 0)
        and (not block_runtime or runtime_skip == 0)
    )

    payload = {
        "run_id": run_id,
        "manifest_hash": manifest.get("manifest_hash", ""),
        "spec_hash": (manifest.get("spec") or {}).get("hash", ""),
        "status_counts": {
            "passed": int(summary.get("passed") or 0),
            "failed": failed,
            "skipped": int(summary.get("skipped") or 0),
            "errors": errors,
            "manual": manual,
            "runtime_binding": runtime_skip,
        },
        "test_result": "failed" if test_code == EXIT_TEST_FAILED else (
            "error" if test_code not in (0, EXIT_TEST_FAILED) else "passed"),
        "backflow": backflow,
        "scope_integrity": True,
        "manual_pending": manual,
        "runtime_binding_pending": runtime_skip,
        "release_ready": release_ready,
    }
    write_json(records_root(cfg) / run_id / "summary.json", payload)
    print(f"汇总: {payload['test_result']} | 回流={backflow} | manual={manual} | release_ready={release_ready}")
    return payload


def _execute(args, cfg: dict, paths: dict, run_id: str) -> int:
    execute_cfg = cfg.get("execute") or {}
    providers = load_providers()
    harness_cfg = providers.get("harness") or {}
    provider = str(harness_cfg.get("provider") or "harness")
    pipe_cfg = qa_casepipe.casepipe_config(providers)
    generation_cfg = cfg.get("generation") or {}
    gen_mode = "spec" if args.from_spec else str(generation_cfg.get("mode") or "harness").lower()
    spec_mode = gen_mode == "spec"

    print("\n" + "=" * 62)
    print(f"DSH 测试工作流 · run_id = {run_id}")
    print(f"UI 模式: {cfg['tests'].get('ui_mode', 'local')} | 用例生成: "
          f"{provider}{'（已接入）' if _harness_enabled() else '（未接入）'} | DB: "
          f"{'启用' if (cfg.get('db') or {}).get('enabled') else '未启用(预留)'}")
    if pipe_cfg["enabled"]:
        print(f"qa-toolkit 回填: 已启用（spec={pipe_cfg['spec']}，Excel={pipe_cfg['xlsx']}）")
    print("=" * 62)

    # 1 用例生成
    if args.skip_gen:
        print("\n▶ 阶段[1 用例生成]: 已跳过（--skip-gen）")
    elif spec_mode:
        _run("1 用例生成(spec 转换)", ["scripts/spec_to_cases.py", "--run-id", run_id])
    else:
        policy = str(execute_cfg.get("harness_failure_policy", "abort")).lower()
        code = _run("1 用例生成", ["scripts/case_gen/harness_adapter.py"], allow_fail=policy == "continue")
        if code != 0:
            print("（harness 生成失败，当前策略为 continue，继续沿用已有用例）")

    # 1b token 只渲染到运行期临时副本
    if args.token_file:
        rendered = render_api_case(cfg, run_id, args.token_file)
        os.environ["DSH_API_CASE_FILE"] = str(rendered)
        os.environ["DSH_RENDERED_BY_PIPELINE"] = "1"

    # 2/3 manifest + 硬门禁
    manifest = {}
    if spec_mode:
        _run("2 生成 scope-manifest", ["scripts/scope_manifest.py", "--run-id", run_id])
        manifest_path = scope_manifest_path(cfg, run_id)
        os.environ["DSH_SCOPE_MANIFEST"] = str(manifest_path)
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        _run("3 硬门禁", ["scripts/pipeline_gate.py", "--manifest", str(manifest_path)])
    else:
        print("\n▶ 阶段[2/3 scope-manifest/硬门禁]: 非 spec 模式，跳过")

    # 5 执行前审计
    run_audit.init_audit(cfg, providers, paths, run_id, args.skip_gen)

    # 4 格式校验（按 manifest 范围）
    _run("4 格式校验", ["scripts/validate_cases.py"])

    # UI 覆盖度
    _run("UI 覆盖度审计", ["scripts/ui_coverage.py"], allow_fail=True)

    # 6 执行
    test_code = _run("6 执行测试+记录", ["scripts/run_tests.py", "--only", args.only], allow_fail=True)

    # 7 执行后审计
    audit = run_audit.finalize_audit(cfg, providers, paths, run_id)
    print(f"\n执行范围审计: 限制项 {len(audit['limitations'])} 条 | "
          f"中途变更 {len(audit['case_changes']['mid_run'])} 条 | "
          f"跨批次变更 {len(audit['case_changes']['vs_previous'])} 条")

    if test_code == 3:
        print("\nEGO 浏览器 CDP 连接失败，流水线以退出码 3 结束")
        return 3
    if test_code not in (0, EXIT_TEST_FAILED):
        print(f"\n执行阶段异常退出（{test_code}），流水线中止")
        return EXIT_PIPELINE_FAILED

    # 8 报告
    _run("8 测试报告", ["scripts/make_report.py"])
    _run("8b qa-toolkit 扩展能力", ["scripts/qa_extended.py"], allow_fail=True)

    # 9/10 回流
    backflow = {"db": "skipped", "asset": "skipped"}
    if args.no_db:
        print("\n▶ 阶段[9 MySQL 回流]: 已跳过（--no-db）")
    elif not (cfg.get("db") or {}).get("enabled"):
        print("\n▶ 阶段[9 MySQL 回流]: 跳过（db.enabled=false）")
    else:
        cmd = ["scripts/db_sync.py"] + (["--dry-run"] if args.dry_db else [])
        code = _run("9 MySQL 回流", cmd, allow_fail=True)
        backflow["db"] = ("dry-run" if args.dry_db else "ok") if code == 0 else "failed"

    asset_cmd = ["scripts/asset_registry.py"]
    if (cfg.get("db") or {}).get("enabled") and not args.no_db and not args.dry_db:
        asset_cmd.append("--sync-db")
    code = _run("10 资产台账回流", asset_cmd, allow_fail=True)
    if args.no_db:
        backflow["asset"] = "skipped" if code == 0 else "failed"
    else:
        backflow["asset"] = ("dry-run" if args.dry_db else "ok") if code == 0 else "failed"

    summary = _summary(cfg, run_id, test_code, backflow, manifest, args.only)
    code = 0
    if test_code == EXIT_TEST_FAILED:
        code |= EXIT_TEST_FAILED
    if any(v == "failed" for v in backflow.values()):
        code |= EXIT_BACKFLOW_FAILED
    if summary.get("manual_pending") and ((cfg.get("pipeline") or {}).get("release") or {}).get(
            "block_on_manual_pending", True):
        code |= EXIT_MANUAL_PENDING

    print("\n" + "=" * 62)
    if code == 0:
        print(f"DSH 流水线完成：通过（run_id={run_id}）")
    else:
        print(f"DSH 流水线完成：存在未通过项（run_id={run_id}, exit={code}，详见报告）")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="DSH 全自动一键流水线")
    parser.add_argument("--skip-gen", action="store_true", help="跳过用例生成")
    parser.add_argument("--from-spec", action="store_true", help="强制使用 spec 转换")
    parser.add_argument("--new-run", action="store_true", help="强制创建新批次（忽略 DSH_RUN_ID）")
    parser.add_argument("--token-file", default="", help="API token 文件，仅渲染到运行期临时副本")
    parser.add_argument("--dry-db", action="store_true", help="入库只演练")
    parser.add_argument("--only", choices=["functional", "api", "ui", "all"], default="all")
    parser.add_argument("--no-db", action="store_true", help="本次不入库")
    parser.add_argument("--no-lock", action="store_true", help="禁用执行锁（仅调试）")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    run_id = init_run_id(force=args.new_run)
    os.environ["DSH_RUN_ID"] = run_id

    lock_cfg = ((cfg.get("pipeline") or {}).get("lock") or {})
    lock_enabled = bool(lock_cfg.get("enabled", True)) and not args.no_lock
    lock_path = _lock_path(cfg)
    if lock_enabled:
        acquired, info = acquire_pipeline_lock(
            lock_path, run_id, int(lock_cfg.get("stale_after_sec") or 7200)
        )
        if not acquired:
            print(f"已有流水线在执行: {info}", file=sys.stderr)
            return EXIT_PIPELINE_FAILED

    try:
        return _execute(args, cfg, paths, run_id)
    except StageError as exc:
        print(f"\n{exc}，流水线中止", file=sys.stderr)
        return EXIT_PIPELINE_FAILED
    finally:
        if os.environ.get("DSH_RENDERED_BY_PIPELINE") == "1":
            rendered = os.environ.get("DSH_API_CASE_FILE", "")
            if rendered:
                Path(rendered).unlink(missing_ok=True)
        if lock_enabled:
            release_pipeline_lock(lock_path, run_id)


if __name__ == "__main__":
    sys.exit(main())
