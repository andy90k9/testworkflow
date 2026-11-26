#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 全自动一键流水线。

编排完整七个环节：
  ①-③ 用例生成（DeepSeek Harness）
      格式校验（run_id 注入 + Schema 校验）
  ④ 执行测试（pytest API + Playwright UI，支持 CDP 接管 EGO 浏览器）
  ⑤ 执行记录（records/<run_id>/）
  ⑥ 测试报告（reports/report-<run_id>.md / .json）
      附：qa_casepipe.enabled=true 时，报告阶段附带 qa-toolkit 场景层聚合与 Excel 状态回填
  ⑦ MySQL 回流（db.enabled: true 时自动入库）

退出码：0 全部通过；1 有用例失败；2 流程异常中断；3 EGO CDP 连不上。

用法:
  python3 scripts/run_pipeline.py                # 全自动
  python3 scripts/run_pipeline.py --skip-gen     # 跳过 harness 生成（用例已就绪时）
  python3 scripts/run_pipeline.py --dry-db       # 入库阶段只演练不写库
  python3 scripts/run_pipeline.py --only api     # 执行阶段只跑接口
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, ensure_dirs, init_run_id, inject_run_id_into_cases,
    load_providers, WORKFLOW_ROOT,
)
import qa_casepipe  # noqa: E402


def _run(stage: str, cmd: list, allow_fail: bool = False) -> int:
    print(f"\n{'=' * 62}\n▶ 阶段[{stage}]: {' '.join(cmd)}\n{'=' * 62}")
    proc = subprocess.run([sys.executable] + cmd, cwd=str(WORKFLOW_ROOT))
    code = proc.returncode
    if code != 0 and not allow_fail:
        print(f"\n阶段[{stage}] 失败（退出码 {code}），流水线中止")
        sys.exit(2)
    return code


def _harness_enabled() -> bool:
    try:
        return bool((load_providers().get("harness") or {}).get("enabled"))
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="DSH 全自动一键流水线")
    parser.add_argument("--skip-gen", action="store_true", help="跳过 harness 用例生成")
    parser.add_argument("--dry-db", action="store_true", help="入库只演练（dry-run）")
    parser.add_argument("--only", choices=["api", "ui", "all"], default="all")
    parser.add_argument("--no-db", action="store_true", help="本次不入库")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    run_id = init_run_id(force=True)
    execute_cfg = cfg.get("execute") or {}
    providers = load_providers()
    harness_cfg = providers.get("harness") or {}
    provider = str(harness_cfg.get("provider") or "harness")
    pipe_cfg = qa_casepipe.casepipe_config(providers)
    print("\n" + "=" * 62)
    print(f"DSH 测试工作流 · run_id = {run_id}")
    print(f"UI 模式: {cfg['tests'].get('ui_mode', 'local')} | 用例生成: "
          f"{provider}{'（已接入）' if _harness_enabled() else '（未接入）'} | DB: "
          f"{'启用' if (cfg.get('db') or {}).get('enabled') else '未启用(预留)'}")
    if pipe_cfg["enabled"]:
        print(f"qa-toolkit 回填: 已启用（spec={pipe_cfg['spec']}，Excel={pipe_cfg['xlsx']}）")
    print("=" * 62)

    # ①-③ 用例生成
    if args.skip_gen:
        print("\n▶ 阶段[①-③ 用例生成]: 已跳过（--skip-gen）")
    else:
        policy = str(execute_cfg.get("harness_failure_policy", "abort")).lower()
        allow_fail = policy == "continue"
        code = _run("①-③ 用例生成", ["scripts/case_gen/harness_adapter.py"], allow_fail=allow_fail)
        if code != 0:
            print("（harness 生成失败，当前策略为 continue，继续沿用已有用例）")

    # 统一注入 run_id，保证手写用例也绑定本次批次
    inject_run_id_into_cases(cfg, paths, run_id)

    # 格式校验
    _run("格式校验", ["scripts/validate_cases.py"])

    # ④⑤ 执行测试 + 记录（用例失败返回 1，属正常分支）
    test_code = _run("④⑤ 执行测试+记录", ["scripts/run_tests.py", "--only", args.only], allow_fail=True)
    if test_code == 3:
        print("\nEGO 浏览器 CDP 连接失败，流水线以退出码 3 结束")
        sys.exit(3)
    if test_code not in (0, 1):
        print(f"\n执行阶段异常退出（{test_code}），流水线中止")
        sys.exit(2)

    # ⑥ 测试报告
    _run("⑥ 测试报告", ["scripts/make_report.py"])

    # ⑦ MySQL 回流
    if args.no_db:
        print("\n▶ 阶段[⑦ MySQL 回流]: 已跳过（--no-db）")
    elif not (cfg.get("db") or {}).get("enabled"):
        print("\n▶ 阶段[⑦ MySQL 回流]: 跳过（db.enabled=false，预留入口已就绪）")
        print("   配置 config/mysql.env + workflow.yaml→db.columns 后改 enabled: true 即自动回流")
    else:
        cmd = ["scripts/db_sync.py"] + (["--dry-run"] if args.dry_db else [])
        if _run("⑦ MySQL 回流", cmd, allow_fail=True) != 0:
            print("（数据库回流失败：已保留本地 records/ 与 reports/ 兜底数据）")

    print("\n" + "=" * 62)
    if test_code == 0:
        print(f"DSH 流水线完成：全部用例通过（run_id={run_id}）")
        sys.exit(0)
    print(f"DSH 流水线完成：存在失败用例（run_id={run_id}，详见报告）")
    sys.exit(1)


if __name__ == "__main__":
    main()
