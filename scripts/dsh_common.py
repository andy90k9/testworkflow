#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 工作流公共工具模块。"""
import fnmatch
import hashlib
import json
import os
import re
import socket
import sys
import time
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
DEFAULT_RECORDS_DIR = "records"
DEFAULT_TEMP_ROOT = "../dsh-workflow-tmp"


def records_root(cfg: dict = None) -> Path:
    """当前批次根目录；配置可指向仓库外临时目录。"""
    cfg = cfg if cfg is not None else load_config()
    return resolve(str(cfg.get("records_dir") or DEFAULT_RECORDS_DIR))


def run_id_file(cfg: dict = None) -> Path:
    return records_root(cfg) / "current-run.json"


def temp_root(cfg: dict = None) -> Path:
    """仓库外同层级临时根目录，用于缓存/中间产物，不进分支代码。"""
    cfg = cfg if cfg is not None else load_config()
    configured = ((cfg.get("paths") or {}).get("temp_root") or DEFAULT_TEMP_ROOT)
    return resolve(str(configured))


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(obj):
    """递归展开配置中的 ${VAR} / ${VAR:-default} 占位符。"""
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str):
        def repl(match):
            name = match.group(1)
            default = match.group(2)
            value = os.environ.get(name)
            if value not in (None, ""):
                return value
            if default is not None:
                return default
            return match.group(0)
        return _ENV_PATTERN.sub(repl, obj)
    return obj


def load_config() -> dict:
    """读取工作流总配置。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = _expand_env(yaml.safe_load(f) or {})
    providers_file = cfg.get("providers_file")
    if providers_file and not cfg.get("_providers"):
        p = resolve(providers_file)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                cfg["_providers"] = _expand_env(yaml.safe_load(f) or {})
    return cfg


def load_providers() -> dict:
    """读取外部 Provider 配置（Harness / EGO 浏览器）。"""
    if not PROVIDERS_PATH.exists():
        return {}
    with open(PROVIDERS_PATH, "r", encoding="utf-8") as f:
        return _expand_env(yaml.safe_load(f) or {})


def resolve(path_str: str) -> Path:
    """把配置中的相对路径解析为基于工作流根目录的绝对路径。"""
    p = Path(path_str)
    return p if p.is_absolute() else (WORKFLOW_ROOT / p).resolve()


def now_stamp(fmt: str = "%Y%m%d-%H%M%S") -> str:
    return datetime.now().strftime(fmt)


def read_run_id() -> str | None:
    """读取当前批次 run_id；环境变量优先，其次是当前批次指针。"""
    current = os.environ.get("DSH_RUN_ID")
    if current:
        return current
    path = run_id_file()
    if not path.exists():
        return None
    try:
        data = read_json(path)
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
    write_json(run_id_file(), {"run_id": run_id, "created_at": datetime.now().isoformat(timespec="seconds")})
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


def selected_case_files(directory: Path, cfg: dict = None, use_manifest: bool = True) -> list:
    """按 manifest / spec_hash 筛选本轮用例，避免跨项目陈旧用例混入。

    优先读 DSH_SCOPE_MANIFEST；未设置时按转换报告 spec_hash 过滤。
    DSH_CASE_SELECTION=off 可临时关闭筛选。
    """
    cfg = cfg if cfg is not None else load_config()
    selection = ((cfg.get("cases") or {}).get("selection") or {})
    override = os.environ.get("DSH_CASE_SELECTION")
    enabled = selection.get("enabled", True) if override is None else override
    if str(enabled).strip().lower() in ("0", "false", "no", "off", "disabled"):
        return collect_yaml_files(directory)

    manifest_env = os.environ.get("DSH_SCOPE_MANIFEST")
    if use_manifest and manifest_env:
        manifest_path = resolve(manifest_env)
        if not manifest_path.exists():
            print(f"[case-selection] manifest 不存在: {manifest_path}", file=sys.stderr)
            return []
        try:
            manifest = read_json(manifest_path) or {}
        except Exception as exc:
            print(f"[case-selection] manifest 读取失败: {exc}", file=sys.stderr)
            return []
        directory = directory.resolve()
        result = []
        for paths in (manifest.get("case_files") or {}).values():
            for item in paths or []:
                path = resolve(str(item))
                if path.parent == directory:
                    result.append(path)
        return sorted(set(result))

    files = collect_yaml_files(directory)
    report_path = resolve(str(selection.get("spec_report") or "reports/spec-conversion-report.json"))
    expected = ""
    if report_path.exists():
        try:
            expected = str((read_json(report_path) or {}).get("spec_hash") or "")
        except Exception:
            expected = ""
    if not expected:
        print(f"[case-selection] 未找到有效 spec_hash（{report_path}），本轮不筛选用例", file=sys.stderr)
        return files

    include_globs = selection.get("include_globs") or []
    exclude_globs = selection.get("exclude_globs") or []
    selected = []
    for path in files:
        try:
            rel = str(path.resolve().relative_to(WORKFLOW_ROOT))
        except ValueError:
            rel = str(path)
        if include_globs and not any(fnmatch.fnmatch(rel, g) for g in include_globs):
            continue
        if any(fnmatch.fnmatch(rel, g) for g in exclude_globs):
            continue
        try:
            data = load_yaml_file(path)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        if any(isinstance(item, dict) and item.get("spec_bundle_hash") == expected for item in items):
            selected.append(path)
    return selected


def canonical_hash(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def scope_manifest_path(cfg: dict, run_id: str) -> Path:
    template = str(((cfg.get("cases") or {}).get("selection") or {}).get(
        "manifest_template") or "").strip()
    if template:
        return resolve(template.format(run_id=run_id))
    return records_root(cfg) / run_id / "scope-manifest.json"


def mask_url(value: str) -> str:
    """脱敏 URL 中的 userinfo 与 query 值，保留结构便于审计。"""
    text = str(value or "")
    text = re.sub(r"(?i)(https?://)([^/@\s]+)@", r"\1***@", text)
    return re.sub(r"([?&][^=&#\s]+)=([^&#\s]*)", r"\1=***", text)


def acquire_pipeline_lock(path: Path, run_id: str, stale_after_sec: int = 7200) -> tuple:
    """获取执行锁；返回 (acquired, info)。stale lock 会被清理。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if path.exists():
        try:
            info = read_json(path) or {}
        except Exception:
            info = {}
        pid = int(info.get("pid") or 0)
        started = float(info.get("started_ts") or 0)
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive and (not started or now - started < stale_after_sec):
            return False, info
        try:
            path.unlink()
        except OSError:
            pass
    info = {
        "pid": os.getpid(),
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "started_ts": now,
        "host": socket.gethostname(),
    }
    write_json(path, info)
    return True, info


def release_pipeline_lock(path: Path, run_id: str) -> None:
    if not path.exists():
        return
    try:
        info = read_json(path) or {}
    except Exception:
        info = {}
    if str(info.get("run_id") or "") != str(run_id):
        return
    try:
        path.unlink()
    except OSError:
        pass


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
