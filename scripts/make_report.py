#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 测试报告生成：Markdown + 结构化 JSON。

用法:
  python3 scripts/make_report.py
  python3 scripts/make_report.py --run run-20261126-153000
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import load_config, ensure_dirs, read_json, write_json  # noqa: E402

STATUS_CN = {"passed": "通过", "failed": "失败", "errors": "错误", "skipped": "跳过"}


def _fmt_sec(sec) -> str:
    try:
        return f"{float(sec):.2f}s"
    except (TypeError, ValueError):
        return "-"


def _load_details(run_dir: Path) -> list:
    items = []
    for name in ("api-detail.json", "ui-detail.json"):
        p = run_dir / name
        if p.exists():
            for it in read_json(p):
                it["suite"] = it.get("suite") or name.split("-")[0]
                items.append(it)
    return items


def _suite_table(summary: dict) -> str:
    lines = ["| 测试类型 | 总数 | 通过 | 失败 | 错误 | 跳过 | 耗时 | 备注 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for kind, s in summary.get("suites", {}).items():
        st = s.get("stats", {})
        note = s.get("env_skipped") or "-"
        lines.append(
            f"| {kind.upper()} | {st.get('total', 0)} | {st.get('passed', 0)} | {st.get('failed', 0)} "
            f"| {st.get('errors', 0)} | {st.get('skipped', 0)} | {_fmt_sec(st.get('duration_sec'))} | {note} |"
        )
    return "\n".join(lines)


def _detail_section(kind: str, items: list) -> str:
    items = [it for it in items if it.get("suite") == kind]
    if not items:
        return ""
    lines = [f"### {kind.upper()} 用例明细", "",
             "| 用例 | 结果 | 耗时 | 失败/跳过原因 | 截图 |", "| --- | --- | --- | --- | --- |"]
    for it in items:
        status = STATUS_CN.get(it.get("status"), it.get("status"))
        msg = (it.get("message") or "").replace("|", "\\|").replace("\n", " ")[:200]
        shot = it.get("screenshot_path") or "-"
        lines.append(f"| {it.get('case_id')} | {status} | {_fmt_sec(it.get('time'))} | {msg or '-'} | {shot} |")
    return "\n".join(lines) + "\n"


def _fail_section(items: list) -> str:
    fail_items = [it for it in items if it.get("status") in ("failed", "errors")]
    if not fail_items:
        return "本次执行无失败用例。"
    lines = []
    for it in fail_items:
        lines.append(f"#### [{str(it.get('suite', '')).upper()}] {it.get('case_id')}")
        msg = (it.get("message") or "").replace("\n", " ")[:800]
        lines.append(f"- 失败信息: {msg}")
        if it.get("screenshot_path"):
            lines.append(f"- 失败截图: {it['screenshot_path']}")
        if it.get("req_content"):
            lines.append(f"- 请求报文: {str(it['req_content'])[:500]}")
        if it.get("resp_content"):
            lines.append(f"- 响应报文: {str(it['resp_content'])[:500]}")
        detail = it.get("assert_detail")
        if detail:
            lines.append(f"- 断言详情: {json.dumps(detail, ensure_ascii=False)[:500]}")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="DSH 测试报告生成")
    parser.add_argument("--run", help="指定 run_id，如 run-20261126-153000；缺省取最近一次")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    records_dir = paths["records"]

    if args.run:
        run_dir = records_dir / (args.run if str(args.run).startswith("run-") else f"run-{args.run}")
    else:
        latest = records_dir / "latest.json"
        if not latest.exists():
            print("还没有执行记录，请先运行 python3 scripts/run_tests.py")
            raise SystemExit(1)
        run_dir = Path(read_json(latest)["run_dir"])

    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        print(f"未找到执行摘要: {summary_path}")
        raise SystemExit(1)
    summary = read_json(summary_path)
    o = summary["overall"]
    details = _load_details(run_dir)

    total = o["total"] or 1
    pass_rate = f"{o['passed'] / total * 100:.1f}%"

    report = [
        f"# DSH 测试报告 · {summary['run_id']}", "",
        f"- 执行时间: {summary['timestamp']}",
        f"- 接口地址: {summary.get('config', {}).get('api_base_url', '-')}",
        f"- 前端地址: {summary.get('config', {}).get('ui_base_url', '-')}", "",
        "## 一、执行概要", "",
        "| 指标 | 数值 |", "| --- | --- |",
        f"| 用例总数 | {o['total']} |",
        f"| 通过 | {o['passed']} |",
        f"| 失败 | {o['failed']} |",
        f"| 错误 | {o['errors']} |",
        f"| 跳过 | {o['skipped']} |",
        f"| **通过率** | **{pass_rate}** |",
        f"| 总耗时 | {_fmt_sec(o['duration_sec'])} |", "",
        "## 二、分类统计", "", _suite_table(summary), "",
    ]
    for kind in ("api", "ui"):
        section = _detail_section(kind, details)
        if section:
            report.append(section)
    report.extend(["## 三、失败用例分析", "", _fail_section(details), "",
                   "## 四、结论与建议", ""])
    if o["failed"] + o["errors"] == 0:
        report.append("本次执行全部通过，可进入发布评估环节。")
    else:
        report.append(f"本次执行存在 {o['failed'] + o['errors']} 个失败/错误用例，"
                      f"建议优先排查 P0/P1 用例，并核对失败明细中的报错信息。")
    report.append("")

    run_id = summary["run_id"]
    report_path = paths["reports"] / f"report-{run_id}.md"
    report_path.write_text("\n".join(report), encoding="utf-8")

    structured = {
        "run_id": run_id,
        "summary": {**o, "pass_rate": pass_rate, "duration_text": _fmt_sec(o["duration_sec"])},
        "config": summary.get("config", {}),
        "pass_list": [it for it in details if it.get("status") == "passed"],
        "fail_list": [it for it in details if it.get("status") in ("failed", "errors")],
        "skip_list": [it for it in details if it.get("status") == "skipped"],
        "module_distribution": {},
    }
    json_path = paths["reports"] / f"report-{run_id}.json"
    write_json(json_path, structured)

    write_json(paths["reports"] / "latest.json",
               {"report_path": str(report_path), "report_json_path": str(json_path), "run_id": run_id})

    print(f"测试报告已生成: {report_path}")
    print(f"结构化报告已生成: {json_path}")
    print(f"  通过率 {pass_rate}（{o['passed']}/{o['total']}）")


if __name__ == "__main__":
    main()
