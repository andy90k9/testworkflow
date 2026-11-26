#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行范围与用例变更审计（v1.3）。

解决三类可信度缺口：
1. --skip-gen / harness 未接入时，报告必须显式披露“用例不是本轮生成的”；
2. 用例在流水线中途被改动（尤其是断言被放宽）必须留下 before/after 记录；
3. UI skill 动作数量、Playwright 脚本“仅生成未执行”、功能层只机判 checks 等
   范围限制统一从实测数据生成，不靠人工在报告里补写。

产物（均在 records/<run_id>/ 下）：
  run-context.json   批次启动时的生成方式/输入/UI 模式快照
  case-baseline.json 用例断言基线（pre_execution / post_execution）
  audit.json         范围限制 + 变更记录，供 make_report 渲染
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, load_providers, ensure_dirs, read_json, write_json,
    collect_yaml_files, load_yaml_file, resolve_run_dir,
)

VERSION = "1.4.0"
BASELINE_NAME = "case-baseline.json"
CONTEXT_NAME = "run-context.json"
AUDIT_NAME = "audit.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _api_expectations(expect: dict) -> list:
    """与 assert_detail 对齐：status_code / contains / json_path→equals。"""
    expect = expect or {}
    pairs = []
    if "status_code" in expect:
        pairs.append(["status_code", _norm(expect["status_code"])])
    if "contains" in expect:
        pairs.append(["contains", _norm(expect["contains"])])
    if expect.get("json_path"):
        pairs.append([str(expect["json_path"]), _norm(expect.get("equals"))])
    return sorted(pairs)


def snapshot_cases(paths: dict) -> dict:
    """把三类用例的“可机判契约”归一化成可 diff 的快照。"""
    result = {}
    groups = (
        ("api", "api_cases"),
        ("functional", "functional"),
        ("ui", "ui_cases"),
    )
    for case_type, path_key in groups:
        for path in collect_yaml_files(paths[path_key]):
            if path.name.startswith("example."):
                continue
            data = load_yaml_file(path) or []
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict) or not item.get("case_id"):
                    continue
                case_id = str(item["case_id"])
                if case_type == "api":
                    pairs = _api_expectations(item.get("expect") or {})
                elif case_type == "functional":
                    pairs = []
                    for idx, chk in enumerate(item.get("checks") or [], 1):
                        name = chk.get("name") or f"check-{idx}"
                        if chk.get("command"):
                            pairs.append([
                                name,
                                "cmd:" + str(chk.get("command"))
                                + "|expect_exit=" + str(chk.get("expect_exit_code", 0)),
                            ])
                        else:
                            expect = chk.get("expect") or {}
                            for key in sorted(expect.keys()):
                                pairs.append([f"{name}.{key}", _norm(expect[key])])
                else:
                    pairs = [
                        ["step." + str(idx), str(
                            step.get("action") if isinstance(step, dict) else step or ""
                        )]
                        for idx, step in enumerate(item.get("steps") or [], 1)
                    ]
                result[case_id] = {
                    "case_type": case_type,
                    "run_id": item.get("run_id"),
                    "expectations": pairs,
                    "sha1": hashlib.sha1(_norm(item).encode("utf-8")).hexdigest()[:16],
                }
    return result


def write_baseline(run_dir: Path, run_id: str, snapshot: dict,
                   stage: str, note: str = "") -> None:
    path = run_dir / BASELINE_NAME
    data = read_json(path) if path.exists() else {"run_id": run_id, "stages": []}
    stages = [s for s in (data.get("stages") or []) if s.get("stage") != stage]
    stages.append({
        "stage": stage,
        "at": _now(),
        "note": note,
        "cases": snapshot,
    })
    data["run_id"] = run_id
    data["stages"] = stages
    write_json(path, data)


def write_context(run_dir: Path, context: dict) -> None:
    write_json(run_dir / CONTEXT_NAME, context)


def _read_details(run_dir: Path) -> dict:
    return {
        kind: read_json(run_dir / f"{kind}-detail.json") or []
        for kind in ("api", "ui", "functional")
        if (run_dir / f"{kind}-detail.json").exists()
    }


def _api_expectations_from_details(run_dir: Path) -> dict:
    """从上一批次的实际断言明细反推契约，兼容没有基线的历史批次。"""
    result = {}
    for item in _read_details(run_dir).get("api", []):
        pairs = [
            [str(a.get("name")), _norm(a.get("expected"))]
            for a in (item.get("assert_detail") or [])
        ]
        result[str(item.get("case_id"))] = sorted(pairs)
    return result


def _current_api_expectations(snapshot: dict) -> dict:
    return {
        case_id: sorted(item.get("expectations") or [])
        for case_id, item in snapshot.items()
        if item.get("case_type") == "api"
    }


def _diff_expectations(before: dict, after: dict, label: str) -> list:
    changes = []
    for case_id in sorted(set(before) & set(after)):
        if before[case_id] != after[case_id]:
            changes.append({
                "source": label,
                "case_id": case_id,
                "before": before[case_id],
                "after": after[case_id],
            })
    return changes


def _previous_run_dir(run_dir: Path) -> Path | None:
    candidates = [
        p for p in run_dir.parent.iterdir()
        if p.is_dir() and p.name.startswith("run-") and p != run_dir
    ]
    return max(candidates, key=lambda p: p.name) if candidates else None


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.rglob("*") if p.is_file() and not p.name.startswith("."))


def build_context(cfg: dict, providers: dict, run_id: str, skip_gen: bool) -> dict:
    harness = (providers.get("harness") or {})
    pipe_cfg = providers.get("qa_casepipe") or {}
    requirements = resolve_paths(cfg)["requirements"]
    source = resolve_paths(cfg)["source"]
    harness_on = bool(harness.get("enabled"))
    qa_on = bool(pipe_cfg.get("enabled"))
    if skip_gen:
        case_source = "existing_manual_cases"
        reason = "本次使用 --skip-gen，沿用 cases/ 下已有用例"
    elif not harness_on and not qa_on:
        case_source = "existing_manual_cases"
        reason = "harness/qa_casepipe 均未启用，沿用 cases/ 下已有用例"
    else:
        case_source = str(harness.get("provider") or "harness")
        reason = "由用例生成引擎产出"
    return {
        "run_id": run_id,
        "audited_by": f"dsh-workflow/run_audit v{VERSION}",
        "started_at": _now(),
        "workflow_version": str((cfg.get("workflow") or {}).get("version") or "-"),
        "skip_gen": bool(skip_gen),
        "generation": {
            "harness_enabled": harness_on,
            "qa_casepipe_enabled": qa_on,
            "provider": str(harness.get("provider") or "harness"),
            "case_source": case_source,
            "case_source_reason": reason,
            "requirements_files": _count_files(requirements),
            "source_files": _count_files(source),
            "run_id_injected": True,
        },
        "ui_mode": str((cfg.get("tests") or {}).get("ui_mode") or "local"),
        "playwright_enabled": bool((cfg.get("playwright") or {}).get("enabled")),
        "playwright_inject_auth": bool((cfg.get("playwright") or {}).get("inject_auth")),
    }


def resolve_paths(cfg: dict) -> dict:
    """避免为每个调用都重新 mkdir 的轻量路径解析。"""
    from dsh_common import resolve
    return {
        "requirements": resolve(cfg["project"]["requirements_dir"]),
        "source": resolve(cfg["project"]["source_dir"]),
    }


def init_audit(cfg: dict, providers: dict, paths: dict, run_id: str,
               skip_gen: bool) -> dict:
    run_dir = resolve_run_dir(paths["records"], run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    context = build_context(cfg, providers, run_id, skip_gen)
    write_context(run_dir, context)
    write_baseline(run_dir, run_id, snapshot_cases(paths), "pre_execution",
                   "格式校验完成后、执行前的用例契约基线")
    return context


def build_audit(cfg: dict, providers: dict, paths: dict, run_id: str) -> dict:
    run_dir = resolve_run_dir(paths["records"], run_id)
    context_path = run_dir / CONTEXT_NAME
    context = read_json(context_path) if context_path.exists() else build_context(
        cfg, providers, run_id, True
    )
    snapshot = snapshot_cases(paths)
    write_baseline(run_dir, run_id, snapshot, "post_execution",
                   "执行完成后的用例契约快照")

    baseline_path = run_dir / BASELINE_NAME
    baseline = read_json(baseline_path) if baseline_path.exists() else {"stages": []}
    stages = {s.get("stage"): s for s in (baseline.get("stages") or [])}
    pre = (stages.get("pre_execution") or {}).get("cases") or {}
    post = (stages.get("post_execution") or {}).get("cases") or {}
    mid_changes = [
        {
            "source": "mid_run_baseline_diff",
            "case_id": case_id,
            "before_sha1": pre[case_id].get("sha1"),
            "after_sha1": post[case_id].get("sha1"),
        }
        for case_id in sorted(set(pre) & set(post))
        if pre[case_id].get("sha1") != post[case_id].get("sha1")
    ]

    previous = _previous_run_dir(run_dir)
    prev_changes = []
    if previous is not None:
        prev_changes = _diff_expectations(
            _api_expectations_from_details(previous),
            _current_api_expectations(post),
            f"vs_previous_run:{previous.name}",
        )

    details = _read_details(run_dir)
    ui_items = details.get("ui", [])
    skill_actions = sum(len(it.get("skill_results") or []) for it in ui_items)
    screenshots = sum(1 for it in ui_items if it.get("screenshot_path"))
    func_items = details.get("functional", [])
    checks = sum(len(it.get("assert_detail") or []) for it in func_items)

    bugs = read_json(run_dir / "bugs.json") or [] if (run_dir / "bugs.json").exists() else []
    open_bugs = [
        str(b.get("bug_id")) for b in bugs
        if str(b.get("status") or "") not in ("closed", "resolved", "rejected")
    ]

    pw_manifest = {}
    pw_path = run_dir / "playwright" / "playwright-manifest.json"
    if pw_path.exists():
        pw_manifest = read_json(pw_path) or {}

    limitations = []
    generation = context.get("generation") or {}
    if context.get("skip_gen") or generation.get("case_source") == "existing_manual_cases":
        limitations.append(
            "本轮未从需求生成用例（skip-gen 或生成引擎未接入）；"
            f"用例来源为 {generation.get('case_source')}，仅验证现有 cases/ 覆盖范围"
        )
    if not generation.get("requirements_files") and not generation.get("source_files"):
        limitations.append("project/requirements 与 project/src 均无输入，需求→用例链路未验证")
    if str(context.get("ui_mode")) in ("ego_skill", "cdp"):
        limitations.append("UI 用例依赖人工完成的 SSO 登录态，登录态失效时 UI 结果不可复现")
    if ui_items and skill_actions == 0:
        limitations.append("本轮 UI 用例未执行任何 skill 动作，EGO Skill 级能力未验证")
    if pw_manifest:
        limitations.append(
            f"Playwright 脚本仅生成归档（{len(pw_manifest.get('files') or [])} 个文件），"
            "流水线未执行；脚本中的认证值为占位 token"
        )
    if func_items:
        limitations.append(
            f"功能（场景层）仅机判 checks 字段（本轮 {checks} 条断言），"
            "自然语言 steps 未逐条机判"
        )
    if mid_changes:
        limitations.append(f"执行期间有 {len(mid_changes)} 条用例被改动，见 case_changes.mid_run")
    if prev_changes:
        limitations.append(
            f"与上一批次断言相比有 {len(prev_changes)} 条用例契约变化，见 case_changes.vs_previous"
        )
    if open_bugs:
        limitations.append(f"存在未闭环缺陷：{', '.join(open_bugs)}")

    return {
        "run_id": run_id,
        "generated_at": _now(),
        "audited_by": f"dsh-workflow/run_audit v{VERSION}",
        "context": context,
        "generation": generation,
        "execution_scope": {
            "ui_mode": context.get("ui_mode"),
            "ui_skill_actions": skill_actions,
            "ui_screenshots": screenshots,
            "functional_checks_executed": checks,
            "playwright_scripts_generated": len(pw_manifest.get("files") or []),
            "playwright_scripts_executed": 0,
            "playwright_executed_note": "生成器只归档产物，未调用 playwright test",
        },
        "case_changes": {
            "mid_run": mid_changes,
            "vs_previous": prev_changes,
            "previous_run": previous.name if previous is not None else None,
        },
        "open_bugs": open_bugs,
        "limitations": limitations,
        "case_counts": {
            kind: sum(1 for v in post.values() if v.get("case_type") == kind)
            for kind in ("api", "ui", "functional")
        },
    }


def finalize_audit(cfg: dict, providers: dict, paths: dict, run_id: str) -> dict:
    audit = build_audit(cfg, providers, paths, run_id)
    write_json(resolve_run_dir(paths["records"], run_id) / AUDIT_NAME, audit)
    return audit


def backfill(run_id: str) -> dict:
    cfg = load_config()
    providers = load_providers()
    paths = ensure_dirs(cfg)
    return finalize_audit(cfg, providers, paths, run_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="DSH 执行范围与用例变更审计")
    parser.add_argument("--backfill", action="store_true",
                        help="为已有批次补写 audit.json（不重跑测试）")
    parser.add_argument("--run", help="批次 run_id，缺省取 records/latest.json")
    args = parser.parse_args()

    if not args.backfill:
        raise SystemExit("审计由 run_pipeline 自动调用；手工补录请用 --backfill")

    cfg = load_config()
    paths = ensure_dirs(cfg)
    run_id = args.run
    if not run_id:
        latest = paths["records"] / "latest.json"
        if not latest.exists():
            raise SystemExit("未找到 records/latest.json，请用 --run 指定批次")
        run_id = read_json(latest).get("run_id")
    if not run_id:
        raise SystemExit("未找到 run_id")
    audit = backfill(run_id)
    print(f"审计产物已生成: {resolve_run_dir(paths['records'], run_id) / AUDIT_NAME}")
    print(f"  限制项 {len(audit['limitations'])} 条 | 中途变更 "
          f"{len(audit['case_changes']['mid_run'])} 条 | 跨批次变更 "
          f"{len(audit['case_changes']['vs_previous'])} 条")


if __name__ == "__main__":
    main()
