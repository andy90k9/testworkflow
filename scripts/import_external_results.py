#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部执行结果导入器：把其它执行通道的结果接进 dsh-workflow 的账本。

背景：DSH 生态里存在另一套用例账本（例如 e2e 工具的 cases.json / report.json），
它们用 `e2e_run_case` 之类的工具执行，结果只落在自己的 report.json 里，
既不进 records/，也不回流 MySQL——于是出现「报告 50 条、库里 38 条」。

本脚本做两件事：
1. 用例定义：把外部用例库中**尚未进入 cases/** 的用例转成 dsh-workflow 规范的
   YAML，落到 cases/functional/（command 型用例没有可执行断言，按场景层承载，
   步骤记录真实执行的命令与预期）。
2. 执行结果：把外部报告里的结果转成 dsh 明细格式，写入
   records/<run_id>/functional-detail.json（source=external），并同步更新
   summary.json 的 overall 统计——db_sync 回流与 make_report 报告都会带上它们。

用法：
  python3 scripts/import_external_results.py \
      --report /path/to/e2e/report.json --cases /path/to/e2e/cases.json --run <run_id>
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import load_config, ensure_dirs, read_json, write_json, resolve_run_dir  # noqa: E402
from case_aggregate import load_case_index, stats_of  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None

STATUS_MAP = {
    "passed": "passed", "pass": "passed", "ok": "passed",
    "failed": "failed", "fail": "failed",
    "error": "errors", "errors": "errors", "blocked": "errors",
    "skipped": "skipped", "skip": "skipped",
    "notrun": "skipped", "not_run": "skipped", "not-run": "skipped",
}

PRIORITY_MAP = {
    "p0": "P0", "critical": "P0", "blocker": "P0",
    "p1": "P1", "high": "P1",
    "p2": "P2", "medium": "P2", "normal": "P2",
    "p3": "P3", "low": "P3",
}


def _load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {"cases": data} if isinstance(data, list) else data


def _case_list(data):
    cases = data.get("cases")
    if isinstance(cases, list):
        return cases
    if isinstance(cases, dict):
        return list(cases.values())
    return []


def _existing_case_ids(paths):
    """已存在的用例 id（functional/api/ui 三类目录，跳过 example.*）。"""
    return set(load_case_index(paths).keys())


def _to_functional_case(case, run_id):
    """外部 command 型用例 → dsh-workflow functional（场景层）用例。"""
    case_id = case.get("caseId") or case.get("id") or case.get("case_id")
    if not case_id:
        return None
    command = case.get("command") or case.get("cmd") or case.get("run") or ""
    expected = case.get("expected") or case.get("expect") or ""
    if not isinstance(expected, str):
        expected = json.dumps(expected, ensure_ascii=False)
    tags = case.get("tags") or []
    module = str(tags[0]) if isinstance(tags, list) and tags else "external"
    action = f"执行命令: {command}" if command else str(case.get("title") or case_id)
    return {
        "case_id": case_id,
        "run_id": run_id,
        "title": case.get("title") or case_id,
        "module": module,
        "priority": PRIORITY_MAP.get(str(case.get("priority") or "P1").lower(), "P1"),
        "precondition": case.get("precondition") or "",
        "related_req": case.get("relatedReq") or case.get("related_req") or "",
        "steps": [{"step": 1, "action": action, "expected": expected or "执行结果符合预期"}],
        "expect": expected or "",
        "source": "external",
    }


def _to_detail(case):
    case_id = case.get("caseId") or case.get("id") or case.get("case_id")
    if not case_id:
        return None
    status = STATUS_MAP.get(str(case.get("status") or "").strip().lower(), "skipped")
    failure = case.get("failure")
    if isinstance(failure, str):
        message = failure
    elif isinstance(failure, dict):
        message = str(failure.get("message") or failure.get("error")
                      or json.dumps(failure, ensure_ascii=False))
    else:
        message = ""
    if not message:
        message = str(case.get("expected") or "")[:200]
    try:
        seconds = round(float(case.get("durationMs") or 0) / 1000, 3)
    except (TypeError, ValueError):
        seconds = 0.0
    return {
        "case_id": case_id,
        "status": status,
        "time": seconds,
        "message": message[:2000],
        "suite": "functional",
        "source": "external",
        "external": {
            "title": case.get("title"),
            "expected": case.get("expected"),
            "command": case.get("command") or case.get("cmd"),
        },
    }


def _api_ui_details(run_dir):
    items = []
    for name in ("api-detail.json", "ui-detail.json"):
        path = Path(run_dir) / name
        if path.exists():
            items.extend(read_json(path))
    return items


# 状态严重度：越靠前越差，用于判断是否覆盖已有记录
_STATUS_RANK = {"errors": 3, "failed": 3, "passed": 1, "skipped": 0}


def main():
    parser = argparse.ArgumentParser(description="外部执行结果导入器")
    parser.add_argument("--report", required=True, help="外部执行报告 JSON（cases[].caseId/status）")
    parser.add_argument("--cases", help="外部用例库 JSON（用于补齐用例定义）")
    parser.add_argument("--run", help="目标 run_id；缺省取最近一次")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容")
    args = parser.parse_args()

    if yaml is None:
        raise SystemExit("缺少依赖 PyYAML，请先执行: pip install PyYAML")

    cfg = load_config()
    paths = ensure_dirs(cfg)
    report = _load(args.report)
    cases = _load(args.cases) if args.cases else {"cases": []}

    run_id = args.run
    if not run_id:
        latest = Path(paths["records"]) / "latest.json"
        if not latest.exists():
            raise SystemExit("找不到最近批次，请用 --run 指定 run_id")
        run_id = read_json(latest).get("run_id")
    run_dir = resolve_run_dir(paths["records"], run_id)
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise SystemExit(f"批次缺少 summary.json: {run_dir}")

    case_defs = {c.get("caseId") or c.get("id"): c for c in _case_list(cases)}
    results = _case_list(report)
    existing = _existing_case_ids(paths)
    run_id = read_json(summary_path).get("run_id") or Path(run_dir).name

    new_cases = []
    for case_id, case in case_defs.items():
        if case_id and case_id not in existing:
            converted = _to_functional_case(case, run_id)
            if converted:
                new_cases.append(converted)

    details = [d for d in (_to_detail(c) for c in results) if d]

    if args.dry_run:
        print(f"目标批次: {run_id}（{run_dir}）")
        print(f"外部报告结果: {len(details)} 条")
        print(f"待新增用例定义: {len(new_cases)} 条 -> {paths['functional']}")
        for case in new_cases[:20]:
            print(f"  + {case['case_id']} | {str(case['title'])[:40]}")
        return

    if new_cases:
        out_path = Path(paths["functional"]) / f"external-{run_id}.yaml"
        out_path.write_text(yaml.safe_dump(new_cases, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        print(f"用例定义已写入: {out_path}（{len(new_cases)} 条）")

    detail_path = Path(run_dir) / "functional-detail.json"
    merged = {}
    for item in (read_json(detail_path) if detail_path.exists() else []):
        if isinstance(item, dict) and item.get("case_id"):
            merged[item["case_id"]] = item
    imported = 0
    updated = 0
    # 本批次已存在的用例（api/ui 明细 + functional 明细）都算已存在，避免重复计数
    known = {d.get("case_id") for d in _api_ui_details(run_dir)} | set(merged.keys())
    for detail in details:
        prev = merged.get(detail["case_id"])
        if prev is None:
            if detail["case_id"] in known:
                # 该用例已由本批次的 api/ui 通道执行过，不重复计入
                continue
            merged[detail["case_id"]] = detail
            imported += 1
        elif _STATUS_RANK.get(detail["status"], 0) > _STATUS_RANK.get(prev.get("status"), 0):
            # 本批次已有该用例记录，仅当外部结果更差时覆盖，避免重复计数
            merged[detail["case_id"]] = detail
            updated += 1
    merged_list = list(merged.values())
    write_json(detail_path, merged_list)
    print(f"执行明细已写入: {detail_path}（共 {len(merged_list)} 条；新增 {imported}，更新 {updated}）")

    summary = read_json(summary_path)
    overall = summary.setdefault("overall", {})
    executed = _api_ui_details(run_dir) + [d for d in merged_list if d.get("source") != "derived"]
    stats = stats_of(executed)
    overall["total"] = stats["total"]
    overall["passed"] = stats["passed"]
    overall["failed"] = stats["failed"]
    overall["errors"] = stats["errors"]
    overall["skipped"] = stats["skipped"]
    external_seconds = sum(float(d.get("time") or 0) for d in merged_list if d.get("source") != "derived")
    overall["duration_sec"] = round(float(overall.get("duration_sec") or 0) + external_seconds, 2)
    write_json(summary_path, summary)
    print(f"summary.overall 已更新: 总数 {overall['total']} | 通过 {overall['passed']} | "
          f"失败 {overall['failed']} | 错误 {overall['errors']} | 跳过 {overall['skipped']}")
    print(f"场景层派生结果: {sum(1 for d in merged_list if d.get('source') == 'derived')} 条（不计入 overall）")


if __name__ == "__main__":
    main()
