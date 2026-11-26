#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 工作流只读预检与步骤计划生成。

不启动浏览器、不修改文件、不写库、不接收任何凭证。
输出 JSON：{ ok, blockers, warnings, checks, plan }
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import load_config, load_providers, resolve, collect_yaml_files  # noqa: E402
from ego_client import guide_version  # noqa: E402


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.rglob("*") if p.is_file())


def _count_cases(path: Path) -> int:
    n = 0
    for p in collect_yaml_files(path):
        if p.name.startswith("example."):
            continue
        try:
            n += 1
        except Exception:
            pass
    return n


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def build_plan(cfg: dict, checks: dict, only: str, skip_gen: bool) -> list:
    """生成给用户确认的步骤清单。"""
    plan = []

    def add(sid, title, detail, deps=None, status="待执行"):
        plan.append({
            "id": sid,
            "title": title,
            "detail": detail,
            "depends_on": deps or [],
            "status": status,
        })

    add("preflight", "预置条件检查", "只读检查需求、代码、依赖、EGO、Harness 与 MySQL 配置", [])
    add("prepare-target", "准备被测系统", "启动 EGO、打开被测链接，尽力 SSO 登录；失败则交还用户手动登录", ["preflight"])

    harness = checks["harness"]
    if skip_gen or not harness["enabled"]:
        for sid, title in (("gen-functional", "功能用例"), ("gen-api", "API 用例"), ("gen-ui", "UI 用例")):
            add(sid, title + "（跳过）", "不调用 Harness，使用 cases/ 中现有用例", ["prepare-target"], "跳过")
    else:
        add("gen-functional", "生成功能用例", "Harness 基于需求与代码生成 cases/functional/", ["prepare-target"])
        add("gen-api", "生成 API 用例", "Harness 生成 cases/api/，绑定 related_func", ["gen-functional"])
        add("gen-ui", "生成 UI 用例", "Harness 生成 cases/ui/，绑定 related_func 并可用 skill 动作", ["gen-functional"])

    add("validate", "用例格式校验", "校验 run_id、必填字段与 skill 动作白名单",
        ["gen-functional", "gen-api", "gen-ui"])

    if only in ("api", "all"):
        add("execute-api", "执行 API 用例", "requests 发起请求，采集请求/响应/断言明细", ["validate"])
    if only in ("ui", "all"):
        add("execute-ui", "执行 UI 用例", "eg_skill / CDP 模式执行，采集截图与 Skill 结果", ["validate"])

    add("report", "生成测试报告", "产出 Markdown 与结构化 JSON 报告", ["execute-api", "execute-ui"])
    if checks["database"]["enabled"]:
        add("db-sync", "MySQL 回流", "按列映射写入批次、用例、结果与报告表", ["report"])
    else:
        add("db-sync", "MySQL 回流（跳过）", "db.enabled=false，仅保留本地记录与报告", ["report"], "跳过")
    return plan


def main():
    cfg = load_config()
    providers = load_providers()
    wf = cfg.get("workflow", {})
    tests = cfg.get("tests", {})
    db_cfg = cfg.get("db") or {}
    ego = providers.get("ego_browser") or {}
    harness = providers.get("harness") or {}

    requirements = resolve(cfg["project"]["requirements_dir"])
    source = resolve(cfg["project"]["source_dir"])
    cases = {
        "functional": resolve(cfg["cases"]["functional_dir"]),
        "api": resolve(cfg["cases"]["api_dir"]),
        "ui": resolve(cfg["cases"]["ui_dir"]),
    }
    records = resolve(cfg["records_dir"])

    only = os.environ.get("DSH_ONLY", "all")
    skip_gen = os.environ.get("DSH_SKIP_GEN", "false").lower() == "true"

    cli = os.environ.get("DSH_EGO_CLI") or ego.get("cli_path") or "ego-browser"
    import shutil
    cli_path = shutil.which(cli)
    guide_file = resolve(ego.get("skill_guide_cache") or "records/ego-browser-skill-guide.md")
    guide = guide_file.read_text(encoding="utf-8") if guide_file.exists() else ""

    checks = {
        "workflow": {"name": wf.get("name", "dsh"), "version": wf.get("version", "")},
        "requirements": {"path": str(requirements), "exists": requirements.exists(), "files": _count_files(requirements)},
        "source": {"path": str(source), "exists": source.exists(), "files": _count_files(source)},
        "cases": {key: {"path": str(path), "count": _count_cases(path)} for key, path in cases.items()},
        "harness": {
            "enabled": bool(harness.get("enabled")),
            "mode": harness.get("mode", "command"),
            "configured": bool(harness.get("command") or harness.get("http_endpoint")),
        },
        "ego": {
            "cli": cli,
            "cli_path": cli_path,
            "available": bool(cli_path),
            "guide_cached": bool(guide),
            "guide_version": guide_version(guide) if guide else None,
        },
        "ui_mode": tests.get("ui_mode", "local"),
        "python_deps": {
            name: _module_available(name)
            for name in ("yaml", "requests", "pytest")
        },
        "playwright": _module_available("playwright"),
        "database": {
            "enabled": bool(db_cfg.get("enabled")),
            "env_file": str(resolve(db_cfg.get("env_file") or "config/mysql.env")),
            "env_exists": resolve(db_cfg.get("env_file") or "config/mysql.env").exists(),
            "columns_configured": bool(db_cfg.get("columns")),
        },
        "records_dir": {"path": str(records), "exists": records.exists()},
    }

    blockers, warnings = [], []
    if not checks["python_deps"]["yaml"]:
        blockers.append("缺少 Python 依赖 PyYAML")
    if not checks["python_deps"]["requests"]:
        blockers.append("缺少 Python 依赖 requests")
    if not checks["python_deps"]["pytest"]:
        blockers.append("缺少 Python 依赖 pytest")
    if checks["ui_mode"] in ("local", "cdp") and only in ("ui", "all") and not checks["playwright"]:
        blockers.append(f"ui_mode={checks['ui_mode']} 需要 playwright，当前未安装")

    if only in ("ui", "all") and not checks["ego"]["available"]:
        blockers.append(f"未找到 EGO 浏览器命令 `{cli}`")
    if checks["ego"]["available"] and not checks["ego"]["guide_cached"]:
        warnings.append("EGO 运行时指南未缓存，首次执行会自动刷新")

    total_cases = sum(v["count"] for v in checks["cases"].values())
    if not skip_gen:
        if not checks["harness"]["enabled"]:
            if total_cases == 0:
                blockers.append("harness 未接入且 cases/ 无可用用例")
            else:
                warnings.append("harness 未接入，将使用 cases/ 中现有用例")
        elif not checks["harness"]["configured"]:
            blockers.append("harness.enabled=true 但未配置 command/http_endpoint")
        if checks["harness"]["enabled"] and (not checks["requirements"]["files"] or not checks["source"]["files"]):
            blockers.append("harness 生成需要 project/requirements/ 与 project/src/ 非空")
    if checks["database"]["enabled"] and not checks["database"]["env_exists"]:
        blockers.append("db.enabled=true 但 config/mysql.env 不存在")
    if checks["database"]["enabled"] and not checks["database"]["columns_configured"]:
        warnings.append("db.columns 未配置，将使用脚本内置列映射")

    plan = build_plan(cfg, checks, only, skip_gen)
    result = {
        "ok": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "plan": plan,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
