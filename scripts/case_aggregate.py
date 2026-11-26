#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用例关联与功能（场景层）聚合。

dsh-workflow 的三类用例之间存在两种关联方向：

- API / UI 用例通过 ``related_func`` 指向它所属的 functional（场景）用例；
- functional 用例是「场景层」，本身没有可执行断言，状态由关联的 API/UI 结果派生。

本模块负责：
1. 建立用例索引（case_id → 类型/关联/标题等）；
2. 由 API/UI 执行明细派生 functional（场景层）执行明细；
3. 汇总统计口径：真实执行（executed）vs 场景派生（derived）。

派生规则与 qa-case-pipeline 的场景层聚合保持一致：
失败 > 部分通过 > 阻塞 > 通过 > 未执行。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import collect_yaml_files, load_yaml_file  # noqa: E402

STATUS_PASS = "passed"
STATUS_FAIL = "failed"
STATUS_ERROR = "errors"
STATUS_SKIP = "skipped"

# 场景层状态优先级：失败 > 部分通过 > 阻塞 > 通过 > 未执行
SEVERITY_ORDER = (STATUS_FAIL, "partial", STATUS_ERROR, STATUS_PASS, STATUS_SKIP)

# DSH 执行状态 → 场景层状态词表
STATUS_CN = {
    STATUS_PASS: "通过",
    STATUS_FAIL: "失败",
    STATUS_ERROR: "阻塞",
    STATUS_SKIP: "未执行",
}


def load_case_index(paths: dict) -> dict:
    """建立用例索引：case_id → {type, title, module, priority, related_func}。"""
    index: dict = {}
    for key, ctype in (("functional", "functional"), ("api_cases", "api"), ("ui_cases", "ui")):
        directory = paths.get(key)
        if not directory or not Path(directory).exists():
            continue
        for path in collect_yaml_files(Path(directory)):
            if path.name.startswith("example."):
                continue
            try:
                data = load_yaml_file(path)
            except Exception:  # noqa: BLE001
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict) or not item.get("case_id"):
                    continue
                index[item["case_id"]] = {
                    "case_id": item["case_id"],
                    "case_type": ctype,
                    "title": item.get("title"),
                    "module": item.get("module"),
                    "priority": item.get("priority"),
                    "related_func": item.get("related_func"),
                    "related_req": item.get("related_req"),
                    "steps": item.get("steps") or [],
                    "expect": item.get("expect") or {},
                    "request": item.get("request") or {},
                    "precondition": item.get("precondition") or "",
                }
    return index


def _derive(statuses: list) -> tuple[str, str]:
    """由关联用例状态派生场景层状态与说明。"""
    if not statuses:
        return STATUS_SKIP, "无关联 API/UI 用例执行结果"
    if STATUS_FAIL in statuses:
        return STATUS_FAIL, f"存在失败项 {statuses.count(STATUS_FAIL)} 条"
    if STATUS_ERROR in statuses:
        return STATUS_ERROR, f"含阻塞项 {statuses.count(STATUS_ERROR)} 条"
    if STATUS_PASS in statuses:
        return STATUS_PASS, f"关联用例全部通过（{statuses.count(STATUS_PASS)} 条）"
    return STATUS_SKIP, "关联用例均未执行"


def aggregate_functional(index: dict, details: list) -> list:
    """由 API/UI 执行明细派生 functional（场景层）明细。

    返回的明细与 api-detail.json / ui-detail.json 同构，便于报告与回流统一消费。
    """
    # case_id → 执行状态（API/UI 明细）
    status_by_case: dict = {}
    for item in details or []:
        case_id = item.get("case_id")
        if not case_id:
            continue
        status = item.get("status")
        if case_id in status_by_case and status_by_case[case_id] == STATUS_PASS:
            # 已判定失败的结果不被通过覆盖
            continue
        status_by_case[case_id] = status

    # API/UI 用例 → 关联的 functional 用例
    children: dict = {}
    for case_id, meta in index.items():
        if meta["case_type"] not in ("api", "ui"):
            continue
        parent = meta.get("related_func")
        if not parent:
            continue
        children.setdefault(parent, []).append(case_id)

    rows: list = []
    for case_id, meta in index.items():
        if meta["case_type"] != "functional":
            continue
        linked = children.get(case_id, [])
        statuses = [status_by_case[c] for c in linked if c in status_by_case]
        status, summary = _derive(statuses)
        rows.append({
            "case_id": case_id,
            "status": status,
            "time": "0.0",
            "message": summary,
            "suite": "functional",
            "source": "derived",
            "related": linked,
        })
    return rows


def split_details(details: list) -> tuple[list, list]:
    """拆分真实执行明细与场景派生明细。"""
    executed = [d for d in details or [] if d.get("source") != "derived"]
    derived = [d for d in details or [] if d.get("source") == "derived"]
    return executed, derived


def stats_of(details: list) -> dict:
    """按明细统计 total/passed/failed/errors/skipped。"""
    stats = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for item in details or []:
        stats["total"] += 1
        status = item.get("status")
        if status == STATUS_PASS:
            stats["passed"] += 1
        elif status == STATUS_FAIL:
            stats["failed"] += 1
        elif status == STATUS_ERROR:
            stats["errors"] += 1
        else:
            stats["skipped"] += 1
    return stats
