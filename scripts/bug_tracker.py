#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺陷台账：测试过程发现的缺陷要留痕、可流转、可落库。

设计要点：
1. **台账跨批次**：缺陷主表 `records/bugs.json`，同一缺陷在多批次复现时累加
   `occurrences` 与 `seen_runs`，不重复开单。
2. **指纹去重（按根因归并）**：指纹 = sha1(模块 + 归一化错误签名)，归一化会把
   数字/UUID/长 hex 换成占位符，因此「同一模块下同一种失败特征」的多条用例
   归并为同一缺陷，`case_ids` 记录全部关联用例。
3. **状态机**：new → confirmed → fixing → verifying → closed，旁路 rejected，
   closed 可 reopened；非法流转直接拒绝并提示合法后继状态。
4. **流转留痕**：每次状态变更写入 `history`（时间/原状态/新状态/备注/操作人）。
5. **批次快照**：`records/<run_id>/bugs.json` 记录该批次发现或复现的缺陷，
   db_sync 按 run_id 回流，保留批次维度可查性。
"""
import argparse
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import load_config, ensure_dirs, read_json, write_json, resolve_run_dir, now_stamp  # noqa: E402

STATUS_NEW = "new"
STATUS_CONFIRMED = "confirmed"
STATUS_FIXING = "fixing"
STATUS_VERIFYING = "verifying"
STATUS_CLOSED = "closed"
STATUS_REOPENED = "reopened"
STATUS_REJECTED = "rejected"

STATUS_CN = {
    STATUS_NEW: "新建",
    STATUS_CONFIRMED: "已确认",
    STATUS_FIXING: "修复中",
    STATUS_VERIFYING: "待验证",
    STATUS_CLOSED: "已关闭",
    STATUS_REOPENED: "重新打开",
    STATUS_REJECTED: "已拒绝",
}

TRANSITIONS = {
    STATUS_NEW: {STATUS_CONFIRMED, STATUS_REJECTED, STATUS_CLOSED},
    STATUS_CONFIRMED: {STATUS_FIXING, STATUS_REJECTED},
    STATUS_FIXING: {STATUS_VERIFYING, STATUS_CONFIRMED},
    STATUS_VERIFYING: {STATUS_CLOSED, STATUS_FIXING, STATUS_REJECTED},
    STATUS_CLOSED: {STATUS_REOPENED},
    STATUS_REOPENED: {STATUS_FIXING, STATUS_CONFIRMED, STATUS_REJECTED},
    STATUS_REJECTED: {STATUS_REOPENED},
}

SEVERITIES = ("blocker", "critical", "major", "minor", "trivial")
SEVERITY_CN = {
    "blocker": "致命", "critical": "严重", "major": "一般",
    "minor": "轻微", "trivial": "提示",
}
PRIORITY_SEVERITY = {"P0": "blocker", "P1": "critical", "P2": "major", "P3": "minor"}

OPEN_STATUSES = (STATUS_NEW, STATUS_CONFIRMED, STATUS_FIXING, STATUS_VERIFYING, STATUS_REOPENED)


def _now():
    return datetime.now().isoformat(timespec="seconds")


_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{12,}\b")
_NUM_RE = re.compile(r"\d+")


def normalize_message(message: str) -> str:
    """归一化错误消息：抹掉数字/UUID/长 hex 等易变片段，保留失败特征。"""
    text = str(message or "").strip().lower()
    text = _UUID_RE.sub("<uuid>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _NUM_RE.sub("#", text)
    text = re.sub(r"\s+", " ", text)
    return text[:200]


def error_signature(message: str) -> str:
    """错误签名（人可读），用于展示与归并判定。"""
    return normalize_message(message)


def fingerprint(case: dict) -> str:
    """缺陷指纹：同模块 + 同失败特征视为同一根因缺陷。

    注意：刻意不把 case_id 纳入指纹——否则同一根因在不同用例上暴露会被拆成
    多条重复缺陷；关联用例统一记录在 `case_ids`。
    """
    module = str(case.get("module") or "").strip().lower()
    raw = f"{module}|{normalize_message(case.get('message') or '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def next_bug_id(bugs: list, today: str = "") -> str:
    """生成缺陷编号 BUG-<日期>-<当日序号>。"""
    day = today or now_stamp("%Y%m%d")
    prefix = f"BUG-{day}-"
    same_day = [b for b in bugs if str(b.get("bug_id", "")).startswith(prefix)]
    return f"{prefix}{len(same_day) + 1:03d}"


def severity_from_priority(priority: str) -> str:
    return PRIORITY_SEVERITY.get(str(priority or "P2").upper(), "major")


def normalize_steps(steps) -> list:
    """把用例定义里的 steps 归一化为可读文本列表。"""
    out = []
    for item in steps or []:
        if isinstance(item, str):
            if item.strip():
                out.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        idx = item.get("step") or item.get("index") or ""
        action = item.get("action") or item.get("desc") or item.get("description") or ""
        expect = item.get("expected") or item.get("expect") or ""
        text = " ".join(str(x) for x in (idx, action) if str(x).strip())
        if expect:
            text += f"（期望：{expect}）"
        if text.strip():
            out.append(text.strip())
    return out


def format_expected(expect) -> str:
    """把用例定义里的 expect 归一化为可读文本。"""
    if isinstance(expect, str):
        return expect
    if isinstance(expect, dict):
        return "; ".join(f"{k}={v}" for k, v in expect.items())
    if isinstance(expect, list):
        return "; ".join(format_expected(e) for e in expect)
    return ""


def make_bug(case: dict, run_id: str, severity: str = "", title: str = "") -> dict:
    """由失败用例明细生成缺陷记录（含基本要素）。"""
    fp = fingerprint(case)
    case_id = case.get("case_id")
    return {
        "bug_id": "",
        "fingerprint": fp,
        "error_signature": error_signature(case.get("message") or ""),
        "title": title or f"[{case_id}] {case.get('title') or case.get('message') or '用例执行失败'}",
        "severity": severity or severity_from_priority(case.get("priority") or "P2"),
        "priority": case.get("priority") or "P2",
        "module": case.get("module") or "",
        "status": STATUS_NEW,
        "case_id": case_id,
        "case_ids": [case_id] if case_id else [],
        "case_type": case.get("suite") or case.get("case_type") or "",
        "description": (case.get("message") or "")[:2000],
        "precondition": case.get("precondition") or "",
        "steps": normalize_steps(case.get("steps")),
        "expected": format_expected(case.get("expected")) or format_expected(case.get("expect")),
        "actual": case.get("actual") or case.get("message") or "",
        "req_content": case.get("req_content") or "",
        "resp_content": case.get("resp_content") or "",
        "assert_detail": case.get("assert_detail") or [],
        "screenshot_path": case.get("screenshot_path") or "",
        "reporter": case.get("reporter") or "dsh-workflow",
        "assignee": "",
        "first_run_id": run_id,
        "seen_runs": [run_id] if run_id else [],
        "occurrences": 1,
        "found_at": _now(),
        "last_seen_at": _now(),
        "confirmed_at": "",
        "fixed_at": "",
        "closed_at": "",
        "history": [{
            "at": _now(), "from": "", "to": STATUS_NEW,
            "note": (case.get("message") or "自动开单")[:500],
            "by": case.get("reporter") or "dsh-workflow",
        }],
    }


def upsert_from_failures(bugs: list, failures: list, run_id: str) -> tuple[list, list]:
    """把失败/错误用例并入台账：同根因复用（累加复现次数），否则新开单。"""
    created, reused = [], []
    index = {b.get("fingerprint"): b for b in bugs if b.get("fingerprint")}
    for case in failures or []:
        fp = fingerprint(case)
        bug = index.get(fp)
        if bug:
            bug["occurrences"] = int(bug.get("occurrences") or 1) + 1
            if run_id and run_id not in bug.get("seen_runs", []):
                bug.setdefault("seen_runs", []).append(run_id)
            case_id = case.get("case_id")
            if case_id and case_id not in (bug.get("case_ids") or []):
                bug.setdefault("case_ids", []).append(case_id)
            if case.get("screenshot_path") and not bug.get("screenshot_path"):
                bug["screenshot_path"] = case["screenshot_path"]
            bug["last_seen_at"] = _now()
            if bug.get("status") == STATUS_CLOSED:
                bug["status"] = STATUS_REOPENED
                bug.setdefault("history", []).append({
                    "at": _now(), "from": STATUS_CLOSED, "to": STATUS_REOPENED,
                    "note": f"批次 {run_id} 再次复现", "by": "dsh-workflow",
                })
            elif bug.get("status") == STATUS_REJECTED:
                bug["status"] = STATUS_REOPENED
                bug.setdefault("history", []).append({
                    "at": _now(), "from": STATUS_REJECTED, "to": STATUS_REOPENED,
                    "note": f"批次 {run_id} 再次复现，撤销此前拒绝结论", "by": "dsh-workflow",
                })
            reused.append(bug["bug_id"])
            continue
        bug = make_bug(case, run_id)
        bug["bug_id"] = next_bug_id(bugs)
        bugs.append(bug)
        index[fp] = bug
        created.append(bug["bug_id"])
    return created, reused


def change_status(bugs: list, bug_id: str, new_status: str, note: str = "",
                  operator: str = "", assignee: str = "",
                  severity: str = "") -> tuple[bool, str]:
    """缺陷状态流转（含非法流转拦截与留痕）。"""
    bug = next((b for b in bugs if b.get("bug_id") == bug_id), None)
    if not bug:
        return False, f"未找到缺陷: {bug_id}"
    if new_status not in STATUS_CN:
        return False, f"非法状态: {new_status}（可选：{', '.join(STATUS_CN)}）"
    if severity and severity not in SEVERITIES:
        return False, f"非法级别: {severity}（可选：{', '.join(SEVERITIES)}）"
    current = bug.get("status") or STATUS_NEW
    if new_status == current:
        if not (severity or assignee):
            return False, f"缺陷 {bug_id} 已处于 {STATUS_CN[current]}"
        changed = []
        if assignee:
            changed.append(f"处理人 {bug.get('assignee') or '未指派'} → {assignee}")
            bug["assignee"] = assignee
        if severity:
            changed.append(f"级别 {SEVERITY_CN.get(bug.get('severity'), bug.get('severity'))}"
                           f" → {SEVERITY_CN.get(severity, severity)}")
            bug["severity"] = severity
        bug.setdefault("history", []).append({
            "at": _now(), "from": current, "to": current,
            "note": "；".join([n for n in [note] + changed if n]),
            "by": operator or "manual",
        })
        return True, f"{bug_id}：仍为 {STATUS_CN[current]}；" + "；".join(changed)
    allowed = TRANSITIONS.get(current, set())
    if new_status not in allowed:
        legal = "、".join(STATUS_CN[s] for s in sorted(allowed)) or "（终态）"
        return False, (f"不允许的状态流转：{STATUS_CN[current]} → {STATUS_CN[new_status]}；"
                       f"当前合法后继：{legal}")
    bug["status"] = new_status
    if assignee:
        bug["assignee"] = assignee
    if severity:
        bug["severity"] = severity
    stamp = _now()
    bug["last_status_at"] = stamp
    if new_status == STATUS_CONFIRMED:
        bug["confirmed_at"] = stamp
    elif new_status == STATUS_VERIFYING:
        bug["fixed_at"] = stamp
    elif new_status == STATUS_CLOSED:
        bug["closed_at"] = stamp
    extra = []
    if severity:
        extra.append(f"级别调整为 {SEVERITY_CN.get(severity, severity)}")
    bug.setdefault("history", []).append({
        "at": stamp, "from": current, "to": new_status,
        "note": "；".join([n for n in [note] + extra if n]),
        "by": operator or "manual",
    })
    return True, f"{bug_id}：{STATUS_CN[current]} → {STATUS_CN[new_status]}"


def assign(bugs: list, bug_id: str, assignee: str, note: str = "",
           operator: str = "") -> tuple[bool, str]:
    """指派处理人（留痕）。"""
    bug = next((b for b in bugs if b.get("bug_id") == bug_id), None)
    if not bug:
        return False, f"未找到缺陷: {bug_id}"
    old = bug.get("assignee") or ""
    bug["assignee"] = assignee
    bug.setdefault("history", []).append({
        "at": _now(), "from": bug.get("status") or STATUS_NEW, "to": bug.get("status") or STATUS_NEW,
        "note": (note or f"处理人：{old or '未指派'} → {assignee}"),
        "by": operator or "manual",
    })
    return True, f"{bug_id}：处理人 {old or '未指派'} → {assignee}"


def stats(bugs: list) -> dict:
    """台账统计：按状态与严重级别。"""
    by_status = {s: 0 for s in STATUS_CN}
    by_severity = {s: 0 for s in SEVERITIES}
    for bug in bugs or []:
        status = bug.get("status") or STATUS_NEW
        by_status[status] = by_status.get(status, 0) + 1
        sev = bug.get("severity") or "major"
        by_severity[sev] = by_severity.get(sev, 0) + 1
    return {
        "total": len(bugs or []),
        "open": sum(1 for b in bugs or [] if b.get("status") in OPEN_STATUSES),
        "by_status": by_status,
        "by_severity": by_severity,
    }


def load_ledger(paths: dict) -> list:
    """读取全局缺陷台账。"""
    path = Path(paths["records"]) / "bugs.json"
    if not path.exists():
        return []
    data = read_json(path)
    return data if isinstance(data, list) else []


def save_ledger(paths: dict, bugs: list) -> Path:
    path = Path(paths["records"]) / "bugs.json"
    write_json(path, bugs)
    return path


def batch_snapshot(bugs: list, run_id: str) -> list:
    """批次视角的缺陷清单。

    = 本批次发现或复现的缺陷
    + 台账中仍未关闭的缺陷（避免上批次遗留的待修缺陷在本批次丢失）
    """
    rows = []
    for bug in bugs or []:
        touched = (run_id in (bug.get("seen_runs") or [])
                   or bug.get("first_run_id") == run_id)
        if touched or bug.get("status") in OPEN_STATUSES:
            rows.append(bug)
    return rows


def save_batch_snapshot(paths: dict, run_id: str, bugs: list) -> Path:
    run_dir = resolve_run_dir(paths["records"], run_id)
    path = Path(run_dir) / "bugs.json"
    write_json(path, bugs)
    return path


def main():
    parser = argparse.ArgumentParser(description="缺陷台账")
    parser.add_argument("action", choices=["list", "show", "add", "status", "assign",
                                           "stats", "sync"], help="操作")
    parser.add_argument("--bug-id", help="缺陷编号（status/show/assign 操作用）")
    parser.add_argument("--title", help="缺陷标题（add 操作用）")
    parser.add_argument("--severity", choices=SEVERITIES, help="严重级别")
    parser.add_argument("--priority", help="优先级 P0-P3")
    parser.add_argument("--module", help="所属模块")
    parser.add_argument("--case-id", help="关联用例")
    parser.add_argument("--case-type", help="用例类型")
    parser.add_argument("--description", default="", help="缺陷描述")
    parser.add_argument("--expected", default="", help="期望结果")
    parser.add_argument("--actual", default="", help="实际结果")
    parser.add_argument("--steps", default="", help="复现步骤（每行一步）")
    parser.add_argument("--assignee", help="处理人")
    parser.add_argument("--reporter", default="manual", help="报告人")
    parser.add_argument("--note", default="", help="状态变更备注")
    parser.add_argument("--by", default="manual", help="操作人")
    parser.add_argument("--to", dest="new_status", help="目标状态（status 操作用）")
    parser.add_argument("--run", help="批次 run_id")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    bugs = load_ledger(paths)

    if args.action == "list":
        if not bugs:
            print("缺陷台账为空")
            return
        for bug in bugs:
            sev = SEVERITY_CN.get(bug.get("severity"), bug.get("severity"))
            print(f"[{bug.get('bug_id')}] {STATUS_CN.get(bug.get('status'), bug.get('status'))} "
                  f"| {sev} | {bug.get('title')}")
            case_ids = bug.get("case_ids") or ([bug.get("case_id")] if bug.get("case_id") else [])
            print(f"    用例={','.join(str(c) for c in case_ids)} 模块={bug.get('module') or '-'} "
                  f"复现={bug.get('occurrences')} 次 处理人={bug.get('assignee') or '-'}")
        return

    if args.action == "show":
        bug = next((b for b in bugs if b.get("bug_id") == args.bug_id), None)
        if not bug:
            print(f"未找到缺陷: {args.bug_id}")
            raise SystemExit(1)
        fields = [
            ("缺陷编号", bug.get("bug_id")),
            ("标题", bug.get("title")),
            ("严重级别", SEVERITY_CN.get(bug.get("severity"), bug.get("severity"))),
            ("优先级", bug.get("priority")),
            ("所属模块", bug.get("module") or "-"),
            ("当前状态", STATUS_CN.get(bug.get("status"), bug.get("status"))),
            ("关联用例", ", ".join(str(c) for c in (bug.get("case_ids") or [])) or "-"),
            ("用例类型", bug.get("case_type") or "-"),
            ("缺陷描述", bug.get("description") or "-"),
            ("前置条件", bug.get("precondition") or "-"),
            ("复现步骤", " / ".join(str(s) for s in (bug.get("steps") or [])) or "-"),
            ("期望结果", bug.get("expected") or "-"),
            ("实际结果", bug.get("actual") or "-"),
            ("请求内容", bug.get("req_content") or "-"),
            ("响应内容", bug.get("resp_content") or "-"),
            ("断言明细", bug.get("assert_detail") or "-"),
            ("截图证据", bug.get("screenshot_path") or "-"),
            ("报告人", bug.get("reporter") or "-"),
            ("处理人", bug.get("assignee") or "-"),
            ("复现次数", bug.get("occurrences")),
            ("首见批次", bug.get("first_run_id") or "-"),
            ("出现批次", ", ".join(bug.get("seen_runs") or []) or "-"),
            ("发现时间", bug.get("found_at") or "-"),
            ("最近出现", bug.get("last_seen_at") or "-"),
            ("确认时间", bug.get("confirmed_at") or "-"),
            ("修复时间", bug.get("fixed_at") or "-"),
            ("关闭时间", bug.get("closed_at") or "-"),
        ]
        for label, value in fields:
            print(f"{label}: {value}")
        print("流转记录:")
        for item in bug.get("history") or []:
            print(f"  [{item.get('at')}] {STATUS_CN.get(item.get('from'), item.get('from') or '-')}"
                  f" → {STATUS_CN.get(item.get('to'), item.get('to'))}"
                  f" | {item.get('by') or '-'}"
                  + (f" | {item.get('note')}" if item.get("note") else ""))
        return

    if args.action == "stats":
        data = stats(bugs)
        print(f"缺陷总数 {data['total']} | 未关闭 {data['open']}")
        print("按状态:", {STATUS_CN[k]: v for k, v in data["by_status"].items() if v})
        print("按级别:", {SEVERITY_CN[k]: v for k, v in data["by_severity"].items() if v})
        return

    if args.action == "add":
        case = {
            "case_id": args.case_id, "suite": args.case_type, "title": args.title,
            "message": args.description or args.actual or args.title,
            "priority": args.priority or "P2", "module": args.module,
            "expected": args.expected, "actual": args.actual,
            "steps": [s for s in (args.steps or "").splitlines() if s.strip()],
            "reporter": args.reporter,
        }
        bug = make_bug(case, args.run or "", severity=args.severity or "", title=args.title or "")
        bug["bug_id"] = next_bug_id(bugs)
        bug["assignee"] = args.assignee or ""
        bugs.append(bug)
        save_ledger(paths, bugs)
        print(f"已登记缺陷 {bug['bug_id']}（{SEVERITY_CN.get(bug['severity'])}）：{bug['title']}")
        return

    if args.action == "status":
        if not args.bug_id or not args.new_status:
            print("status 操作需要 --bug-id 与 --to")
            raise SystemExit(1)
        ok, message = change_status(bugs, args.bug_id, args.new_status, args.note,
                                     args.by, args.assignee or "", args.severity or "")
        if not ok:
            print(message)
            raise SystemExit(1)
        save_ledger(paths, bugs)
        print(message)
        return

    if args.action == "assign":
        if not args.bug_id or not args.assignee:
            print("assign 操作需要 --bug-id 与 --assignee")
            raise SystemExit(1)
        ok, message = assign(bugs, args.bug_id, args.assignee, args.note, args.by)
        if not ok:
            print(message)
            raise SystemExit(1)
        save_ledger(paths, bugs)
        print(message)
        return

    if args.action == "sync":
        if not args.run:
            print("sync 操作需要 --run")
            raise SystemExit(1)
        path = save_batch_snapshot(paths, args.run, batch_snapshot(bugs, args.run))
        print(f"批次缺陷快照已写入: {path}")


if __name__ == "__main__":
    main()
