#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 工作流公共工具模块。"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    print("缺少依赖 PyYAML，请先执行: pip install PyYAML", file=sys.stderr)
    raise

WORKFLOW_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = WORKFLOW_ROOT / "config" / "workflow.yaml"
PROVIDERS_PATH = WORKFLOW_ROOT / "config" / "providers.yaml"
RUN_ID_FILE = WORKFLOW_ROOT / "records" / "current-run.json"


def load_config() -> dict:
    """读取工作流总配置。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    providers_file = cfg.get("providers_file")
    if providers_file and not cfg.get("_providers"):
        p = resolve(providers_file)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                cfg["_providers"] = yaml.safe_load(f) or {}
    return cfg


def load_providers() -> dict:
    """读取外部 Provider 配置（Harness / EGO 浏览器）。"""
    if not PROVIDERS_PATH.exists():
        return {}
    with open(PROVIDERS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve(path_str: str) -> Path:
    """把配置中的相对路径解析为基于工作流根目录的绝对路径。"""
    p = Path(path_str)
    return p if p.is_absolute() else (WORKFLOW_ROOT / p).resolve()


def now_stamp(fmt: str = "%Y%m%d-%H%M%S") -> str:
    return datetime.now().strftime(fmt)


def read_run_id() -> str | None:
    """读取当前批次 run_id，不存在返回 None。"""
    if not RUN_ID_FILE.exists():
        return None
    try:
        data = read_json(RUN_ID_FILE)
        return data.get("run_id") or None
    except Exception:
        return None


def init_run_id(force: bool = False) -> str:
    """获取或新建 run_id；force=True 时强制开启新批次。"""
    current = os.environ.get("DSH_RUN_ID")
    if current and not force:
        return current
    if not force:
        current = read_run_id()
        if current:
            return current
    run_id = f"run-{now_stamp()}"
    write_json(RUN_ID_FILE, {"run_id": run_id, "created_at": datetime.now().isoformat(timespec="seconds")})
    os.environ["DSH_RUN_ID"] = run_id
    return run_id


def resolve_run_dir(records_dir: Path, run_id: str) -> Path:
    """run_id 已是 run-xxx 时直接使用，避免 run-run-xxx。"""
    name = run_id if str(run_id).startswith("run-") else f"run-{run_id}"
    return records_dir / name


def ensure_dirs(cfg: dict) -> dict:
    """确保所有约定目录存在，返回解析后的路径字典。"""
    wf = cfg["workflow"]
    proj = cfg["project"]
    cases = cfg["cases"]
    tests = cfg["tests"]
    paths = {
        "requirements": resolve(proj["requirements_dir"]),
        "source": resolve(proj["source_dir"]),
        "functional": resolve(cases["functional_dir"]),
        "api_cases": resolve(cases["api_dir"]),
        "ui_cases": resolve(cases["ui_dir"]),
        "api_tests": resolve(tests["api_dir"]),
        "ui_tests": resolve(tests["ui_dir"]),
        "functional_tests": resolve(tests.get("functional_dir", "tests/functional")),
        "records": resolve(cfg["records_dir"]),
        "reports": resolve(cfg["reports_dir"]),
    }
    paths["workflow_root"] = WORKFLOW_ROOT
    for key, p in paths.items():
        if key != "workflow_root":
            p.mkdir(parents=True, exist_ok=True)
    return paths


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_yaml_file(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def collect_yaml_files(directory: Path, recursive: bool = True):
    """收集目录下所有 .yaml/.yml 文件（递归），按文件名排序。"""
    if not directory.exists():
        return []
    pattern = "**/*.y*ml" if recursive else "*.y*ml"
    return sorted(directory.glob(pattern))


def inject_run_id_into_cases(cfg: dict, paths: dict, run_id: str) -> None:
    """把当前 run_id 注入三类用例（跳过 example 示例文件）。"""
    for key in ("functional", "api_cases", "ui_cases"):
        for path in collect_yaml_files(paths[key]):
            if path.name.startswith("example."):
                continue
            data = load_yaml_file(path)
            if isinstance(data, list):
                items, is_list = data, True
            elif isinstance(data, dict):
                items, is_list = [data], False
            else:
                continue
            changed = False
            for item in items:
                if isinstance(item, dict) and item.get("run_id") != run_id:
                    item["run_id"] = run_id
                    changed = True
            if changed:
                path.write_text(
                    yaml.safe_dump(items if is_list else items[0], allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )


def safe_case_id(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(case_id))


def mask_sensitive(obj):
    """递归脱敏：password / token / authorization 等字段。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if re.search(r"(password|passwd|token|secret|authorization)", str(k), re.I):
                out[k] = "***MASKED***"
            else:
                out[k] = mask_sensitive(v)
        return out
    if isinstance(obj, list):
        return [mask_sensitive(v) for v in obj]
    return obj
