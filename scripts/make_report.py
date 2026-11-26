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
        f"共 {len(func_items)} 条功能（场景层）用例：真实执行 {len(executed)} 条（机判 checks），"
        f"按关联关系派生 {len(derived)} 条（失败 > 部分通过 > 阻塞 > 通过 > 未执行）。",
        f"统计：通过 {stats['passed']} | 失败 {stats['failed']} | 阻塞 {stats['errors']} | 未执行 {stats['skipped']}",
        "",
        "| 功能用例 | 结果 | 来源 | 说明 | 关联用例 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for it in func_items:
        msg = (it.get("message") or "").replace("|", "\\|").replace("\n", " ")[:120]
        related = ", ".join(it.get("related") or []) or "-"
        source = "派生" if it.get("source") == "derived" else "执行(checks)"
        lines.append(f"| {it.get('case_id')} | {STATUS_CN.get(it.get('status'), it.get('status'))} "
                     f"| {source} | {msg or '-'} | {related} |")
    lines.extend(["", "注：功能用例的 steps 为自然语言描述，未逐条机判；机判范围为 checks 字段，"
                      "UI 渲染由 UI 用例覆盖。"])
    return "\n".join(lines) + "\n"


def _qa_extended_section(data: dict) -> str:
    """qa-toolkit 扩展能力执行结果章节。"""
    steps = (data or {}).get("steps") or []
    if not steps:
        return ""
    lines = [
        "本次调用 qa-toolkit 的扩展能力（登录 / 真实 API 执行 / 模板规范 / 产物审核）：",
        "",
        "| 能力 | 结果 | 说明 |",
        "| --- | --- | --- |",
    ]
    for step in steps:
        detail = str(step.get("error") or step.get("out") or "-").replace("|", "\\|")[:120]
        lines.append(f"| {step.get('name') or '-'} | "
                     f"{'成功' if step.get('ok') else '失败'} | {detail} |")
    lines.append("")
    return "\n".join(lines)


def _playwright_section(manifest: dict) -> str:
    """Playwright 定位脚本产物章节。"""
    if not manifest or manifest.get("mode") == "disabled":
        return ""
    files = manifest.get("files") or []
    if not files:
        return ""
    lines = [
        f"本批次由 `cases/ui/*.yaml` 的 DSL 生成 **{manifest.get('total_cases', 0)}** 条用例的 "
        f"Playwright 定位脚本，共 **{len(files)}** 个文件。"
        f"（生成器：{manifest.get('generator')}，来源：{manifest.get('source')}）",
        "",
        "| 模块 | 脚本 | 用例数 | SHA1 |",
        "| --- | --- | --- | --- |",
    ]
    for row in manifest.get("features") or []:
        lines.append(f"| {row.get('feature') or '-'} | `{row.get('file')}` "
                     f"| {len(row.get('case_ids') or [])} | {row.get('sha1') or '-'} |")
    lines.extend(["", "配套文件：`index.ts`（脚本入口）、`playwright.config.ts`（运行配置）、"
                      "`playwright-manifest.json`（产物清单）。", ""])
    lines.append("**注意：脚本仅生成归档，流水线未执行 Playwright；UI 结果来自 ego_skill 执行器，"
                 "脚本中注入的 token 为占位值，不可当作登录态证明。**")
    return "\n".join(lines)


def _cell(value) -> str:
    text = "-" if value in (None, "") else str(value)
    return text.replace("|", "\\|").replace("\n", " ")[:220]


def _ui_coverage_section(data: dict) -> str:
    """渲染 UI 覆盖度（当前用例集，不等于本轮执行数量）。"""
    if not data:
        return ""
    verbs = "、".join(
        f"{name} {count}" for name, count in (data.get("action_verbs") or {}).items()
    ) or "-"
    lines = [
        "以下统计来自当前 `cases/` 用例集，**不是本轮执行数量**：", "",
        "| 指标 | 数值 |", "| --- | --- |",
        f"| 功能用例 / UI 用例 | {data.get('functional_total', 0)} / {data.get('ui_total', 0)} |",
        f"| 已映射 / 未映射功能用例 | {data.get('mapped_functional', 0)} / {data.get('unmapped_functional', 0)} |",
        f"| 功能→UI 覆盖率 | {data.get('coverage_rate', 0)}% |",
        f"| 交互型 / 仅渲染 UI 用例 | {data.get('interaction_cases', 0)} / {data.get('render_only_cases', 0)} |",
        "", f"动作分布：{verbs}。",
    ]
    return "\n".join(lines) + "\n"


def _scope_section(audit: dict) -> str:
    """把审计产物渲染成人可读的「执行范围与限制」章节。"""
    if not audit:
        return ""
    gen = audit.get("generation") or {}
    scope = audit.get("execution_scope") or {}
    ctx = audit.get("context") or {}
    changes = audit.get("case_changes") or {}
    mid = changes.get("mid_run") or []
    prev = changes.get("vs_previous") or []
    source = gen.get("case_source") or "-"
    if ctx.get("skip_gen"):
        generation_label = "跳过生成（--skip-gen）"
    elif source == "existing_manual_cases":
        generation_label = "生成引擎未接入，沿用现有用例"
    else:
        generation_label = f"生成引擎产出（{source}）"

    rows = [
        ("用例生成", generation_label),
        ("用例来源", source),
        ("需求/代码输入", f"requirements {gen.get('requirements_files', 0)} 个文件；"
                          f"src {gen.get('source_files', 0)} 个文件"),
        ("run_id 注入", "流水线统一注入" if gen.get("run_id_injected") else "-"),
        ("UI 执行通道", f"{scope.get('ui_mode') or '-'}；"
                        f"skill 动作 {scope.get('ui_skill_actions', 0)} 条；"
                        f"截图 {scope.get('ui_screenshots', 0)} 张"),
        ("功能层机判", f"checks {scope.get('functional_checks_executed', 0)} 条；自然语言 steps 未机判"),
        ("Playwright", f"生成 {scope.get('playwright_scripts_generated', 0)} 个脚本；"
                       f"执行 {scope.get('playwright_scripts_executed', 0)} 个"),
        ("用例契约变更", f"执行中 {len(mid)} 条；"
                         f"对比 {changes.get('previous_run') or '上批次'} {len(prev)} 条"),
        ("未闭环缺陷", ", ".join(audit.get("open_bugs") or []) or "-"),
    ]
    lines = [
        "以下内容由审计产物 `audit.json` 自动生成，非人工填写：", "",
        "| 项目 | 实测结果 |", "| --- | --- |",
    ]
    for key, value in rows:
        lines.append(f"| {_cell(key)} | {_cell(value)} |")
    if prev:
        lines.extend(["", "跨批次断言变化用例："
                      + ", ".join(str(c.get("case_id")) for c in prev) + "。"])
    limitations = audit.get("limitations") or []
    if limitations:
        lines.extend(["", "限制项："])
        lines.extend(f"- {item}" for item in limitations)
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
    parser.add_argument("--out-dir", help="报告输出目录（预览用；指定时不覆盖正式报告/latest.json）")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    records_dir = paths["records"]
    out_dir = Path(args.out_dir) if args.out_dir else paths["reports"]
    out_dir.mkdir(parents=True, exist_ok=True)

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

    pw_manifest = {}
    pw_path = run_dir / "playwright" / "playwright-manifest.json"
    if pw_path.exists():
        try:
            pw_manifest = read_json(pw_path) or {}
        except Exception:  # noqa: BLE001
            pw_manifest = {}

    qa_ext = {}
    qa_ext_path = run_dir / "qa-extended.json"
    if qa_ext_path.exists():
        try:
            qa_ext = read_json(qa_ext_path) or {}
        except Exception:  # noqa: BLE001
            qa_ext = {}

    audit = {}
    audit_path = run_dir / "audit.json"
    if audit_path.exists():
        try:
            audit = read_json(audit_path) or {}
        except Exception:  # noqa: BLE001
            audit = {}

    ui_coverage = {}
    ui_cov_path = run_dir / "ui-coverage.json"
    if ui_cov_path.exists():
        try:
            ui_coverage = read_json(ui_cov_path) or {}
        except Exception:  # noqa: BLE001
            ui_coverage = {}

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

    numerals = ("三", "四", "五", "六", "七", "八", "九", "十", "十一")
    extra_sections = []
    scope_section = _scope_section(audit)
    if scope_section:
        extra_sections.append(("执行范围与限制", scope_section))
    ui_cov_section = _ui_coverage_section(ui_coverage)
    if ui_cov_section:
        extra_sections.append(("UI 覆盖度（当前用例集）", ui_cov_section))
    functional_section = _functional_section(details)
    if functional_section:
        extra_sections.append(("功能（场景层）用例统计", functional_section))
    bug_section_text = _bug_section(bugs)
    if bug_section_text:
        extra_sections.append(("缺陷清单（过程发现）", bug_section_text))
    pw_section = _playwright_section(pw_manifest)
    if pw_section:
        extra_sections.append(("Playwright 定位脚本产物", pw_section))
    qa_ext_section = _qa_extended_section(qa_ext)
    if qa_ext_section:
        extra_sections.append(("qa-toolkit 扩展能力", qa_ext_section))
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
        report.append("在本次执行的用例范围内全部通过。")
        limits = audit.get("limitations") or []
        if limits:
            report.append(f"但存在 {len(limits)} 项范围限制，通过率仅代表现有用例与当前环境；"
                          "发布评估前请核对「执行范围与限制」章节，不宜直接等同于全链路通过。")
        else:
            report.append("未发现范围限制，可进入发布评估环节。")
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
    report_path = out_dir / f"report-{run_id}.md"
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
        "playwright_scripts": pw_manifest,
        "qa_toolkit_extended": qa_ext,
        "module_distribution": {},
        "qa_pipeline": casepipe,
        "audit": audit,
        "ui_coverage": ui_coverage,
    }
    json_path = out_dir / f"report-{run_id}.json"
    write_json(json_path, structured)

    if not args.out_dir:
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
