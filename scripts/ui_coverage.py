#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 用例覆盖度审计（v1.4）。

回答三个问题：
1. 42 条功能用例里有多少真正派生了 UI 用例？
2. UI 用例里有多少是“只看文字”，有多少包含 click/fill/download 等交互？
3. 缺口具体落在哪些功能用例上？

产物：
  records/<run_id>/ui-coverage.json
  reports/ui-coverage-<run_id>.md
"""
import argparse
import collections
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, ensure_dirs, read_json, write_json,
    collect_yaml_files, load_yaml_file, read_run_id, resolve_run_dir,
)

INTERACTION_VERBS = {
    "click", "fill", "select", "press", "check", "hover",
    "download", "upload", "popup", "skill", "mouse_click",
}


def _cases(paths: dict, key: str) -> list:
    result = []
    for path in collect_yaml_files(paths[key]):
        if path.name.startswith("example."):
            continue
        data = load_yaml_file(path) or []
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("case_id"):
                result.append(item)
    return result


def _actions(case: dict) -> list:
    result = []
    for step in case.get("steps") or []:
        action = step.get("action") if isinstance(step, dict) else step
        if action:
            result.append(str(action))
    return result


def compute(paths: dict) -> dict:
    functional = _cases(paths, "functional")
    ui = _cases(paths, "ui_cases")
    linked_func = {
        str(case.get("related_func"))
        for case in ui
        if case.get("related_func")
    }
    mapped = [c for c in functional if str(c.get("case_id")) in linked_func]
    unmapped = [c for c in functional if str(c.get("case_id")) not in linked_func]

    verb_counter = collections.Counter()
    interaction_cases = 0
    for case in ui:
        verbs = [a.strip().split(" ", 1)[0].lower() for a in _actions(case)]
        verb_counter.update(verbs)
        if any(v in INTERACTION_VERBS for v in verbs):
            interaction_cases += 1

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "functional_total": len(functional),
        "ui_total": len(ui),
        "mapped_functional": len(mapped),
        "unmapped_functional": len(unmapped),
        "coverage_rate": round(len(mapped) / len(functional) * 100, 1) if functional else 0.0,
        "interaction_cases": interaction_cases,
        "render_only_cases": len(ui) - interaction_cases,
        "ui_by_module": dict(collections.Counter(str(c.get("module") or "-") for c in ui)),
        "action_verbs": dict(verb_counter.most_common()),
        "mapped_case_ids": [str(c.get("case_id")) for c in mapped],
        "unmapped_cases": [
            {"case_id": str(c.get("case_id")), "module": str(c.get("module") or "-"),
             "title": str(c.get("title") or "")}
            for c in unmapped
        ],
        "ui_case_ids": [str(c.get("case_id")) for c in ui],
    }


def render_markdown(data: dict, run_id: str) -> str:
    lines = [
        f"# DSH UI 覆盖度审计 · {run_id}", "",
        "以下统计来自**当前 cases/ 用例集**，不是本轮执行数量；",
        "用例若在本报告生成后被修改，需重新执行本脚本。", "",
        "| 指标 | 数值 |", "| --- | --- |",
        f"| 功能用例总数 | {data['functional_total']} |",
        f"| UI 用例总数 | {data['ui_total']} |",
        f"| 已映射功能用例 | {data['mapped_functional']} |",
        f"| 未映射功能用例 | {data['unmapped_functional']} |",
        f"| 功能→UI 覆盖率 | {data['coverage_rate']}% |",
        f"| 含交互动作的 UI 用例 | {data['interaction_cases']} |",
        f"| 仅渲染断言的 UI 用例 | {data['render_only_cases']} |", "",
        "## 动作分布", "",
        "| 动作 | 次数 |", "| --- | --- |",
    ]
    for verb, count in data["action_verbs"].items():
        lines.append(f"| {verb} | {count} |")
    lines.extend(["", "## 未映射的功能用例", "",
                  "| 用例 | 模块 | 标题 |", "| --- | --- | --- |"])
    for item in data["unmapped_cases"]:
        lines.append(f"| {item['case_id']} | {item['module']} | {item['title']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="DSH UI 用例覆盖度审计")
    parser.add_argument("--run", help="批次 run_id；缺省取 records/current-run.json")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    run_id = args.run or read_run_id()
    if not run_id:
        raise SystemExit("未找到 run_id，请用 --run 指定")

    data = compute(paths)
    run_dir = resolve_run_dir(paths["records"], run_id)
    json_path = run_dir / "ui-coverage.json"
    md_path = paths["reports"] / f"ui-coverage-{run_id}.md"
    write_json(json_path, data)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(data, run_id), encoding="utf-8")

    print(f"UI 覆盖度已生成: {json_path}")
    print(f"覆盖度报告已生成: {md_path}")
    print(f"  功能→UI 覆盖 {data['mapped_functional']}/{data['functional_total']} "
          f"({data['coverage_rate']}%)；交互型用例 {data['interaction_cases']}/{data['ui_total']}")


if __name__ == "__main__":
    main()
