#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 测试执行编排脚本。

流程：
1. 读取统一 run_id，创建 records/<run_id>/
2. 运行 API / UI 测试（pytest + junitxml），透传 CDP 退出码
3. 合并执行器写入的运行时明细（请求/响应/断言/截图/Skill 结果）
4. 汇总 summary.json / api-detail.json / ui-detail.json / latest.json

退出码：0 全通过；1 有用例失败；3 CDP 连不上；2 其他异常。
"""
import argparse
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, load_providers, ensure_dirs, init_run_id, now_stamp,
    resolve_run_dir, read_json, write_json, WORKFLOW_ROOT,
)

RUNTIME_KEYS = (
    "run_id", "request", "response", "req_content", "resp_content",
    "assert_detail", "screenshot_path", "skill_results", "steps",
    "error_msg", "start_time", "end_time", "cost_ms",
)


def _normalize_case_id(name: str) -> str:
    """pytest parametrize 的 junit name 形如 test_api_case[API-AUTH-001]，取真实用例 ID。"""
    text = str(name or "")
    m = re.search(r"\[(.+)\]$", text)
    return m.group(1) if m else text


def _parse_junit(xml_path: Path):
    """解析 pytest junitxml -> (统计 dict, 用例明细 list)。"""
    details = []
    duration = 0.0
    if not xml_path.exists():
        stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0, "duration_sec": 0.0}
        return stats, details
    tree = ET.parse(xml_path)
    for suite in tree.getroot().iter("testsuite"):
        try:
            duration += float(suite.get("time", 0))
        except (TypeError, ValueError):
            pass
        for tc in suite.iter("testcase"):
            item = {
                "case_id": _normalize_case_id(tc.get("name", "")),
                "classname": tc.get("classname", ""),
                "time": tc.get("time", "0"),
                "status": "passed",
                "message": "",
                "suite": "",
            }
            fail = tc.find("failure")
            err = tc.find("error")
            skip = tc.find("skipped")
            if fail is not None:
                item["status"] = "failed"
                item["message"] = (fail.get("message") or "")[:1000]
            elif err is not None:
                item["status"] = "errors"
                item["message"] = (err.get("message") or "")[:1000]
            elif skip is not None:
                item["status"] = "skipped"
                item["message"] = (skip.get("message") or "")[:500]
            details.append(item)

    stats = {
        "total": len(details),
        "passed": sum(1 for d in details if d["status"] == "passed"),
        "failed": sum(1 for d in details if d["status"] == "failed"),
        "skipped": sum(1 for d in details if d["status"] == "skipped"),
        "errors": sum(1 for d in details if d["status"] == "errors"),
        "duration_sec": duration,
    }
    return stats, details


def _merge_runtime(kind: str, run_dir: Path, details: list) -> list:
    """把执行器写出的 runtime/*.json 合并到 junit 明细。"""
    runtime_dir = run_dir / "runtime"
    if not runtime_dir.exists():
        return details
    for path in sorted(runtime_dir.glob(f"{kind}-*.json")):
        try:
            data = read_json(path)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        case_id = data.get("case_id")
        target = next((d for d in details if d.get("case_id") == case_id), None)
        if target is None:
            target = {
                "case_id": case_id or path.stem,
                "classname": kind,
                "time": "0",
                "status": data.get("status", "passed"),
                "message": data.get("error_msg", ""),
                "suite": kind,
            }
            details.append(target)
        target["suite"] = kind
        for key in RUNTIME_KEYS:
            if key in data:
                target[key] = data[key]
    return details


def _run_suite(kind: str, test_dir: Path, run_dir: Path, env_extra: dict):
    """运行一类测试，返回 (stats, details, env_skipped, returncode)。"""
    stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0, "duration_sec": 0.0}
    xml_path = run_dir / f"{kind}-junit.xml"
    log_path = run_dir / f"{kind}-pytest.log"

    env = os.environ.copy()
    env.update(env_extra)
    cmd = [
        sys.executable, "-m", "pytest", str(test_dir),
        "-q", "--tb=line", f"--junitxml={xml_path}",
        "-p", "no:cacheprovider",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(WORKFLOW_ROOT))
    log_path.write_text((proc.stdout or "") + "\n---STDERR---\n" + (proc.stderr or ""), encoding="utf-8")

    if "no tests ran" in (proc.stdout or "").lower():
        return stats, [], f"{kind} 目录下没有可执行用例", 0

    stats, details = _parse_junit(xml_path)
    details = _merge_runtime(kind, run_dir, details)
    if kind == "ui" and details and all(d["status"] == "skipped" for d in details):
        env_skipped = ("playwright 库未安装或 UI 用例已跳过；"
                       "cdp 模式需 EGO 已启动，local 模式需 playwright install chromium")
    else:
        env_skipped = None
    return stats, details, env_skipped, proc.returncode


def main():
    parser = argparse.ArgumentParser(description="DSH 测试执行编排")
    parser.add_argument("--only", choices=["functional", "api", "ui", "all"], default="all")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    tests_cfg = cfg["tests"]
    ego = load_providers().get("ego_browser") or {}

    run_id = init_run_id()
    run_dir = resolve_run_dir(paths["records"], run_id)
    (run_dir / "runtime").mkdir(parents=True, exist_ok=True)
    stamp = run_id.replace("run-", "", 1) if run_id.startswith("run-") else now_stamp()

    summary = {
        "run_id": run_id,
        "timestamp": stamp,
        "config": {
            "api_base_url": tests_cfg.get("api_base_url"),
            "ui_base_url": tests_cfg.get("ui_base_url"),
            "ui_mode": tests_cfg.get("ui_mode", "local"),
        },
        "suites": {},
        "overall": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0, "duration_sec": 0.0},
        "exit_code": 0,
    }
    print(f"▶ 本次执行 run_id: {run_id}")

    base_env = {"DSH_RUN_ID": run_id, "DSH_RUN_DIR": str(run_dir)}
    return_codes = []

    if args.only in ("api", "all"):
        api_stats, api_detail, api_skip, api_rc = _run_suite(
            "api", paths["api_tests"], run_dir,
            {**base_env,
             "DSH_API_BASE_URL": tests_cfg.get("api_base_url", ""),
             "DSH_API_TIMEOUT": str(tests_cfg.get("api_timeout", 15))},
        )
        return_codes.append(api_rc)
        summary["suites"]["api"] = {"stats": api_stats, "env_skipped": api_skip}
        write_json(run_dir / "api-detail.json", api_detail)
        for key in ("total", "passed", "failed", "skipped", "errors", "duration_sec"):
            summary["overall"][key] += api_stats[key]

    if args.only in ("ui", "all"):
        ui_env = {
            **base_env,
            "DSH_UI_BASE_URL": tests_cfg.get("ui_base_url", ""),
            "DSH_UI_TIMEOUT": str(tests_cfg.get("ui_timeout", 10)),
            "DSH_UI_MODE": str(tests_cfg.get("ui_mode", "local")),
            "DSH_UI_CDP_ENDPOINT": str(ego.get("cdp_endpoint") or tests_cfg.get("ui_cdp_endpoint", "http://127.0.0.1:9222")),
            "DSH_UI_HEADLESS": str(tests_cfg.get("ui_headless", True)).lower(),
            "DSH_UI_ATTACH_CONTEXT": str(ego.get("attach_context", "first")),
            "DSH_UI_CLOSE_PAGE_AFTER": str(ego.get("close_page_after", True)).lower(),
            "DSH_EGO_MODE": "ego_skill" if str(tests_cfg.get("ui_mode", "local")).lower() == "ego_skill" else "",
            "DSH_EGO_CLI": str(ego.get("cli_path") or "ego-browser"),
            "DSH_EGO_GUIDE_CACHE": str(ego.get("skill_guide_cache") or "records/ego-browser-skill-guide.md"),
            "DSH_EGO_TIMEOUT": str(ego.get("nodejs_timeout_sec") or 30),
        }
        ui_stats, ui_detail, ui_skip, ui_rc = _run_suite("ui", paths["ui_tests"], run_dir, ui_env)
        return_codes.append(ui_rc)
        summary["suites"]["ui"] = {"stats": ui_stats, "env_skipped": ui_skip}
        write_json(run_dir / "ui-detail.json", ui_detail)
        for key in ("total", "passed", "failed", "skipped", "errors", "duration_sec"):
            summary["overall"][key] += ui_stats[key]

    # ③ 功能（场景层）：有执行器则真实执行；没有则由 API/UI 结果派生
    if args.only in ("functional", "all"):
        func_dir = paths.get("functional_tests")
        has_executor = bool(func_dir) and any(Path(func_dir).glob("test_*.py"))
        if has_executor:
            func_stats, func_detail, func_skip, func_rc = _run_suite(
                "functional", Path(func_dir), run_dir,
                {**base_env,
                 "DSH_API_BASE_URL": tests_cfg.get("api_base_url", ""),
                 "DSH_API_TIMEOUT": str(tests_cfg.get("api_timeout", 15))},
            )
            return_codes.append(func_rc)
            summary["suites"]["functional"] = {"stats": func_stats, "env_skipped": func_skip}
            for row in func_detail:
                row["source"] = "executed"
            write_json(run_dir / "functional-detail.json", func_detail)
            for key in ("total", "passed", "failed", "skipped", "errors", "duration_sec"):
                summary["overall"][key] += func_stats[key]
            print(f"功能（场景层）用例执行: 共 {func_stats['total']} 条 | 通过 {func_stats['passed']} | "
                  f"失败 {func_stats['failed']} | 跳过 {func_stats['skipped']}")
        else:
            try:
                from case_aggregate import aggregate_functional, load_case_index

                index = load_case_index(paths)
                api_detail = read_json(run_dir / "api-detail.json") if (run_dir / "api-detail.json").exists() else []
                ui_detail = read_json(run_dir / "ui-detail.json") if (run_dir / "ui-detail.json").exists() else []
                functional_detail = aggregate_functional(index, list(api_detail) + list(ui_detail))
                if functional_detail:
                    write_json(run_dir / "functional-detail.json", functional_detail)
                    derived = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
                    for row in functional_detail:
                        key = {"passed": "passed", "failed": "failed", "errors": "errors"}.get(row["status"], "skipped")
                        derived[key] += 1
                    print(f"功能（场景层）无执行器，改为按关联关系派生: 共 {len(functional_detail)} 条 | "
                          f"通过 {derived['passed']} | 失败 {derived['failed']} | "
                          f"阻塞 {derived['errors']} | 未执行 {derived['skipped']}")
            except Exception as exc:  # noqa: BLE001
                print(f"功能（场景层）聚合跳过: {exc}")

    o = summary["overall"]
    if 3 in return_codes:
        summary["exit_code"] = 3
    elif any(rc not in (0, 1, 5) for rc in return_codes):
        summary["exit_code"] = 2
    elif o["failed"] + o["errors"] > 0:
        summary["exit_code"] = 1

    write_json(run_dir / "summary.json", summary)
    write_json(paths["records"] / "latest.json", {"run_id": run_id, "run_dir": str(run_dir)})

    print(f"执行完成: 总数 {o['total']} | 通过 {o['passed']} | 失败 {o['failed']} | "
          f"错误 {o['errors']} | 跳过 {o['skipped']} | 耗时 {o['duration_sec']:.1f}s")
    for kind, s in summary["suites"].items():
        if s.get("env_skipped"):
            print(f"  [{kind}] 环境跳过: {s['env_skipped']}")
    print(f"执行记录已存档: {run_dir}")
    sys.exit(summary["exit_code"])


if __name__ == "__main__":
    main()
