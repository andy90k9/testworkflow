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
from dsh_common import load_config, load_providers, ensure_dirs, read_json, resolve, write_json  # noqa: E402
import qa_casepipe  # noqa: E402

STATUS_CN = {"passed": "通过", "failed": "失败", "errors": "错误", "skipped": "跳过"}


def _fmt_sec(sec) -> str:
    try:
        return f"{float(sec):.2f}s"
    except (TypeError, ValueError):
        return "-"


def _load_details(run_dir: Path) -> list:
    items = []
    for name in ("api-detail.json", "ui-detail.json", "functional-detail.json"):
        p = run_dir / name
        if p.exists():
            for it in read_json(p):
                it["suite"] = it.get("suite") or name.split("-")[0]
                items.append(it)
    return items


def _functional_section(items: list) -> str:
    """功能（场景层）用例统计：真实执行结果 + 由关联 API/UI 结果派生的状态。"""
    func_items = [it for it in items if it.get("suite") == "functional"]
    if not func_items:
        return ""
    derived = [it for it in func_items if it.get("source") == "derived"]
    executed = [it for it in func_items if it.get("source") != "derived"]
    stats = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for it in func_items:
        key = {"passed": "passed", "failed": "failed", "errors": "errors"}.get(it.get("status"), "skipped")
        stats[key] += 1
    lines = [
        f"共 {len(func_items)} 条功能（场景层）用例：真实执行 {len(executed)} 条，"
        f"按关联关系派生 {len(derived)} 条（失败 > 部分通过 > 阻塞 > 通过 > 未执行）。",
        f"统计：通过 {stats['passed']} | 失败 {stats['failed']} | 阻塞 {stats['errors']} | 未执行 {stats['skipped']}",
        "",
        "| 功能用例 | 结果 | 来源 | 说明 | 关联用例 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for it in func_items:
        msg = (it.get("message") or "").replace("|", "\\|").replace("\n", " ")[:120]
        related = ", ".join(it.get("related") or []) or "-"
        source = "派生" if it.get("source") == "derived" else "执行"
        lines.append(f"| {it.get('case_id')} | {STATUS_CN.get(it.get('status'), it.get('status'))} "
                     f"| {source} | {msg or '-'} | {related} |")
    return "\n".join(lines) + "\n"


def _trail_label(item: dict) -> str:
    """流转记录的展示文案：区分「创建」「状态流转」「属性调整」。"""
    from bug_tracker import STATUS_CN as BUG_STATUS_CN

    src = item.get("from") or ""
    dst = item.get("to") or ""
    src_label = BUG_STATUS_CN.get(src, src) if src else "创建"
    dst_label = BUG_STATUS_CN.get(dst, dst)
    if src and src == dst:
        return f"{src_label}（属性调整）"
    return f"{src_label}→{dst_label}"


def _bug_section(bugs: list) -> str:
    """缺陷清单：过程发现的缺陷台账（基本要素 + 状态）。"""
    if not bugs:
        return ""
    from bug_tracker import SEVERITY_CN, STATUS_CN, stats

    data = stats(bugs)
    lines = [
        f"共 {data['total']} 条缺陷，未关闭 {data['open']} 条。",
        "按状态：" + " | ".join(f"{STATUS_CN[k]} {v}" for k, v in data["by_status"].items() if v),
        "按级别：" + " | ".join(f"{SEVERITY_CN[k]} {v}" for k, v in data["by_severity"].items() if v),
        "",
        "| 缺陷编号 | 标题 | 级别 | 状态 | 关联用例 | 处理人 | 复现 | 发现时间 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for bug in bugs:
        title = str(bug.get("title") or "").replace("|", "\\|")[:80]
        case_ids = bug.get("case_ids") or ([bug.get("case_id")] if bug.get("case_id") else [])
        lines.append(
            f"| {bug.get('bug_id')} | {title} "
            f"| {SEVERITY_CN.get(bug.get('severity'), bug.get('severity'))} "
            f"| {STATUS_CN.get(bug.get('status'), bug.get('status'))} "
            f"| {', '.join(str(c) for c in case_ids) or '-'} | {bug.get('assignee') or '-'} "
            f"| {bug.get('occurrences') or 1} | {str(bug.get('found_at') or '-')[:19]} |"
        )
    lines.append("")
    lines.append("缺陷明细（基本要素与流转记录）：")
    lines.append("")
    for bug in bugs:
        lines.append(f"**{bug.get('bug_id')}** {bug.get('title')}")
        lines.append("")
        lines.append(f"- 级别 / 优先级：{SEVERITY_CN.get(bug.get('severity'), bug.get('severity'))}"
                     f" / {bug.get('priority') or '-'}　模块：{bug.get('module') or '-'}")
        lines.append(f"- 状态：{STATUS_CN.get(bug.get('status'), bug.get('status'))}"
                     f"　处理人：{bug.get('assignee') or '-'}　复现次数：{bug.get('occurrences') or 1}")
        if bug.get("expected"):
            lines.append(f"- 期望结果：{bug['expected']}")
        if bug.get("actual"):
            lines.append(f"- 实际结果：{bug['actual']}")
        if bug.get("steps"):
            lines.append("- 复现步骤：" + "；".join(str(s) for s in bug["steps"]))
        if bug.get("screenshot_path"):
            lines.append(f"- 截图证据：{bug['screenshot_path']}")
        history = bug.get("history") or []
        if history:
            trail = " → ".join(_trail_label(h) for h in history)
            lines.append(f"- 流转：{trail}")
        lines.append("")
    return "\n".join(lines) + "\n"


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


def _casepipe_section(rows: list, stats: dict) -> str:
    """场景层状态统计（qa-case-pipeline 三层用例的场景聚合）。"""
    if not rows:
        return ""
    summary = " | ".join(f"{status} {stats.get(status, 0)}" for status in qa_casepipe.SCENARIO_PRIORITY)
    lines = [f"状态统计：{summary}", "",
             "| 场景用例 | 链路 | 用例名 | 优先级 | 状态 | 说明 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            f"| {row.get('id')} | {row.get('chain') or '-'} | {row.get('name') or '-'} "
            f"| {row.get('priority') or '-'} | {row.get('status')} | {row.get('detail') or '-'} |"
        )
    return "\n".join(lines) + "\n"


def _casepipe_data(providers: dict, details: list, run_dir: Path, env: str) -> dict:
    """汇总 qa-case-pipeline 场景层状态，并把执行结果回填进 Excel。"""
    pipe = qa_casepipe.casepipe_config(providers)
    data = {"enabled": False}
    if not pipe["enabled"]:
        return data
    if not pipe["spec"]:
        return {"enabled": False, "error": "qa_casepipe.spec 未配置"}
    spec_path = resolve(pipe["spec"])
    if not spec_path.exists():
        return {"enabled": False, "error": f"qa-case-pipeline spec 不存在: {spec_path}"}

    spec = qa_casepipe.load_spec(spec_path)
    rows = qa_casepipe.aggregate_scenarios(spec, details)
    stats = qa_casepipe.scenario_stats(rows)
    xlsx_path = resolve(pipe["xlsx"])
    data = {
        "enabled": True,
        "spec": str(spec_path),
        "md": str(resolve(pipe["md"])),
        "xlsx": str(xlsx_path),
        "scenarios": rows,
        "scenario_stats": stats,
        "fill": {},
    }
    if xlsx_path.exists():
        data["fill"] = qa_casepipe.fill_back(xlsx_path, details, run_dir, env=env, sheets=pipe["sheets"])
    else:
        data["fill"] = {"ok": False, "error": f"Excel 不存在: {xlsx_path}"}
    return data


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

    bugs = []
    bug_path = run_dir / "bugs.json"
    if bug_path.exists():
        try:
            bugs = read_json(bug_path) or []
        except Exception:  # noqa: BLE001
            bugs = []

    providers = load_providers()
    env = str(summary.get("config", {}).get("api_base_url") or "")
    casepipe = _casepipe_data(providers, details, run_dir, env)

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

    casepipe_section = ""
    if casepipe.get("enabled"):
        casepipe_section = _casepipe_section(casepipe.get("scenarios") or [],
                                             casepipe.get("scenario_stats") or {})

    numerals = ("三", "四", "五", "六", "七")
    extra_sections = []
    functional_section = _functional_section(details)
    if functional_section:
        extra_sections.append(("功能（场景层）用例统计", functional_section))
    bug_section_text = _bug_section(bugs)
    if bug_section_text:
        extra_sections.append(("缺陷清单（过程发现）", bug_section_text))
    if casepipe_section:
        extra_sections.append(("场景层状态统计（qa-case-pipeline）", casepipe_section))
    index = 0
    for title, body in extra_sections:
        report.extend([f"## {numerals[index]}、{title}", "", body, ""])
        index += 1
    num_fail = numerals[index]
    num_concl = numerals[index + 1]

    report.extend([f"## {num_fail}、失败用例分析", "", _fail_section(details), "",
                   f"## {num_concl}、结论与建议", ""])
    if o["failed"] + o["errors"] == 0:
        report.append("本次执行全部通过，可进入发布评估环节。")
    else:
        report.append(f"本次执行存在 {o['failed'] + o['errors']} 个失败/错误用例，"
                      f"建议优先排查 P0/P1 用例，并核对失败明细中的报错信息。")
    if casepipe.get("enabled"):
        fill = casepipe.get("fill") or {}
        state = "成功" if fill.get("ok") else f"未完成（{(fill.get('error') or '未知原因')[:200]}）"
        report.append(f"- qa-toolkit 状态回填：{state}，产物 {casepipe.get('xlsx')}")
        report.append(f"- 场景层统计：{json.dumps(casepipe.get('scenario_stats') or {}, ensure_ascii=False)}")
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
        "functional_list": [it for it in details if it.get("suite") == "functional"],
        "bugs": bugs,
        "bug_stats": (__import__("bug_tracker").stats(bugs) if bugs else {}),
        "module_distribution": {},
        "qa_pipeline": casepipe,
    }
    json_path = paths["reports"] / f"report-{run_id}.json"
    write_json(json_path, structured)

    write_json(paths["reports"] / "latest.json",
               {"report_path": str(report_path), "report_json_path": str(json_path), "run_id": run_id})

    print(f"测试报告已生成: {report_path}")
    print(f"结构化报告已生成: {json_path}")
    print(f"  通过率 {pass_rate}（{o['passed']}/{o['total']}）")
    if casepipe.get("enabled"):
        fill = casepipe.get("fill") or {}
        if fill.get("ok"):
            print(f"  qa-toolkit 状态回填成功: {casepipe.get('xlsx')}")
        else:
            print(f"  qa-toolkit 状态回填未完成: {(fill.get('error') or '未知原因')[:200]}")


if __name__ == "__main__":
    main()
