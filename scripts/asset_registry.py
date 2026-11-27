#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用资产台账与核心基线回流（v1.2）。

把本轮使用的所有资产记录到 records/<run_id>/asset-registry.json，
并把 tier=core 的用例冻结为 case-baseline.json；可选同步到 MySQL 两张新表。

所有项目/分支/环境/轮次编码来自配置或运行时，代码不含项目专属值。
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, ensure_dirs, resolve, load_yaml_file, read_json,
    write_json, read_run_id, WORKFLOW_ROOT,
)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return load_yaml_file(path) or {}


def _resolve_var(value, fallback=""):
    text = str(value or "")
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)
    if match:
        return os.environ.get(match.group(1), fallback)
    return text or fallback


def _git_info(enabled: bool) -> tuple:
    if not enabled:
        return "", "", "unknown", "unknown"
    try:
        repo = subprocess.check_output(
            ["git", "-C", str(WORKFLOW_ROOT), "config", "--get", "remote.origin.url"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        repo = ""
    try:
        branch = subprocess.check_output(
            ["git", "-C", str(WORKFLOW_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        commit = subprocess.check_output(
            ["git", "-C", str(WORKFLOW_ROOT), "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        branch, commit = "unknown", "unknown"
    return repo, branch, commit


def _next_seq(records_dir: Path, width: int, run_id: str = "") -> int:
    """同一 run_id 必须得到同一 run_code；仅无时间信息时才用递增序列。"""
    groups = re.findall(r"\d+", str(run_id or ""))
    if groups:
        digits = groups[-1]
        return int(digits[-width:]) if len(digits) >= width else int(digits)
    seq_file = records_dir / ".asset-seq"
    try:
        current = int(seq_file.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        current = 0
    current += 1
    seq_file.parent.mkdir(parents=True, exist_ok=True)
    seq_file.write_text(str(current), encoding="utf-8")
    return current


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_assets(cfg: dict, paths: dict, context: dict) -> list:
    acfg = cfg.get("asset_registry") or {}
    globs = acfg.get("asset_globs") or {}
    excludes = acfg.get("exclude_globs") or []
    version = context["spec_version"] or str((cfg.get("workflow") or {}).get("version") or "")
    assets = []
    seen = set()
    for asset_type, patterns in globs.items():
        for pattern in patterns or []:
            for path in sorted(WORKFLOW_ROOT.glob(str(pattern))):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(WORKFLOW_ROOT))
                if any(fnmatch.fnmatch(rel, str(ex)) for ex in excludes):
                    continue
                if rel in seen:
                    continue
                seen.add(rel)
                identity = "|".join([
                    context["project_code"], context["git_branch"], asset_type, rel, version,
                ])
                assets.append({
                    "asset_id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:24],
                    "asset_type": asset_type,
                    "asset_path": rel,
                    "asset_name": path.name,
                    "asset_version": version,
                    "asset_hash": _sha1_file(path),
                    "source_asset_id": "",
                    "owner": context["project_code"],
                    "status": "active",
                })
    spec_asset = next((a for a in assets if a["asset_type"] == "spec"), None)
    if spec_asset:
        for item in assets:
            if item["asset_type"] != "spec":
                item["source_asset_id"] = spec_asset["asset_id"]
    return assets


def _collect_baseline(cfg: dict, paths: dict, context: dict) -> list:
    acfg = cfg.get("asset_registry") or {}
    bcfg = acfg.get("baseline") or {}
    tier = str(bcfg.get("tier", "core"))
    version_pattern = str(bcfg.get("version_pattern", "baseline-{date}-{seq}"))
    baseline_id = version_pattern.format(
        date=context["date"], seq=context["seq"], project=context["project_code"], env=context["env_code"]
    )
    rows = []
    for key, case_type in (("functional", "functional"), ("api_cases", "api"), ("ui_cases", "ui")):
        for path in sorted(paths[key].glob("*.y*ml")):
            if path.name.startswith("example.") or "legacy" in path.parts:
                continue
            data = load_yaml_file(path) or []
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("tier") or ("core" if str(item.get("priority")) == "P0" else "extension")) != tier:
                    continue
                contract = {
                    "expect": item.get("expect") or {},
                    "steps": item.get("steps") or [],
                    "title": item.get("title") or "",
                }
                rows.append({
                    "baseline_id": baseline_id,
                    "case_id": str(item.get("case_id") or ""),
                    "case_type": case_type,
                    "tier": tier,
                    "is_core": 1,
                    "source_id": str(item.get("source_id") or item.get("related_req") or ""),
                    "source_version": str(item.get("source_version") or context["spec_version"]),
                    "contract_json": contract,
                    "drift_policy": str(item.get("drift_policy") or "snapshot"),
                    "baseline_version": baseline_id,
                    "ext_json": {"source_file": item.get("source_file") or "", "spec_hash": item.get("spec_hash") or ""},
                })
    return rows


def _mysql_env(path: Path) -> dict:
    result = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _json_value(value):
    if value in (None, "", [], {}):
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sync_db(cfg: dict, registry: dict, baselines: list) -> None:
    try:
        import pymysql
    except ImportError:
        print("[警告] 未安装 pymysql，跳过资产台账入库")
        return
    db_cfg = cfg.get("db") or {}
    env = _mysql_env(resolve(str(db_cfg.get("env_file", "config/mysql.env"))))
    if not env:
        print("[警告] 缺少 mysql.env，跳过资产台账入库")
        return
    acfg = cfg.get("asset_registry") or {}
    tables = acfg.get("tables") or {}
    reg_table = str(tables.get("registry", "dsh_asset_registry"))
    base_table = str(tables.get("baseline", "dsh_case_baseline"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def upsert(cursor, table, columns, values, keys):
        placeholders = ", ".join(["%s"] * len(columns))
        updates = ", ".join(f"{c}=VALUES({c})" for c in columns if c not in keys)
        cursor.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {updates or f'{columns[0]}={columns[0]}'}",
            values,
        )

    conn = None
    try:
        conn = pymysql.connect(
            host=env.get("DSH_DB_HOST") or env.get("host") or "127.0.0.1",
            port=int(env.get("DSH_DB_PORT") or env.get("port") or 3306),
            user=env.get("DSH_DB_USER") or env.get("user") or "root",
            password=env.get("DSH_DB_PASSWORD") or env.get("password") or "",
            database=env.get("DSH_DB_NAME") or env.get("database") or "dsh_test",
            charset=env.get("DSH_DB_CHARSET") or "utf8mb4",
        )
        with conn.cursor() as cur:
            for item in registry["assets"]:
                cols = ["asset_id", "project_code", "git_repo", "git_branch", "git_commit",
                        "env_code", "run_code", "run_id", "asset_type", "asset_name", "asset_path",
                        "asset_version", "asset_hash", "source_asset_id", "owner", "status",
                        "created_at", "ext_json"]
                vals = [
                    item["asset_id"], registry["context"]["project_code"], registry["context"]["git_repo"],
                    registry["context"]["git_branch"], registry["context"]["git_commit"],
                    registry["context"]["env_code"], registry["context"]["run_code"],
                    registry["run_id"], item["asset_type"], item["asset_name"], item["asset_path"],
                    item["asset_version"], item["asset_hash"], item["source_asset_id"],
                    item["owner"], item["status"], now, _json_value(registry["context"]),
                ]
                upsert(cur, reg_table, cols, vals, {"run_code", "asset_type", "asset_path"})
            for item in baselines:
                cols = ["baseline_id", "project_code", "git_repo", "git_branch", "env_code", "run_code",
                        "case_id", "case_type", "tier", "is_core", "source_id", "source_version",
                        "contract_json", "drift_policy", "baseline_version", "enabled", "created_at", "ext_json"]
                vals = [
                    item["baseline_id"], registry["context"]["project_code"], registry["context"]["git_repo"],
                    registry["context"]["git_branch"], registry["context"]["env_code"],
                    registry["context"]["run_code"], item["case_id"], item["case_type"], item["tier"],
                    item["is_core"], item["source_id"], item["source_version"],
                    _json_value(item["contract_json"]), item["drift_policy"], item["baseline_version"],
                    1, now, _json_value(item["ext_json"]),
                ]
                upsert(cur, base_table, cols, vals,
                       {"baseline_id", "project_code", "case_id", "baseline_version"})
        conn.commit()
        print(f"资产台账入库完成: {reg_table} {len(registry['assets'])} 行 / {base_table} {len(baselines)} 行")
    except Exception as exc:
        print(f"[警告] 资产台账入库失败（本地产物已保留）: {exc}")
    finally:
        if conn:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="通用资产台账与核心基线回流")
    parser.add_argument("--sync-db", action="store_true", help="同步到 MySQL 新表")
    parser.add_argument("--run-id", help="批次 run_id；缺省读 current-run.json")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    generation = cfg.get("generation") or {}
    acfg = _load_yaml(resolve(str(generation.get("asset_registry_file", "config/asset-registry.yaml"))))
    section = acfg.get("asset_registry") or {}
    cfg["asset_registry"] = section

    run_id = args.run_id or read_run_id()
    if not run_id:
        raise SystemExit("未找到 run_id，请用 --run-id 指定")

    date_fmt = str(section.get("date_format", "%Y%m%d"))
    width = int(section.get("seq_width", 3) or 3)
    match = re.search(r"(\d{8})[-_](\d{6})", str(run_id)) or re.search(r"(\d{8})", str(run_id))
    if match:
        today = match.group(1)
        if len(match.groups()) >= 2 and match.group(2):
            seq = _next_seq(paths["records"], width, match.group(2))
        else:
            digest = hashlib.sha1(run_id.encode("utf-8")).hexdigest()
            seq = int(digest[:8], 16) % (10 ** width)
    else:
        today = datetime.now().strftime(date_fmt)
        digest = hashlib.sha1(run_id.encode("utf-8")).hexdigest()
        seq = int(digest[:8], 16) % (10 ** width)
    repo, branch, commit = _git_info(bool(section.get("git_enabled", True)))
    context = {
        "project_code": _resolve_var(section.get("project_code"), str((cfg.get("workflow") or {}).get("name") or "project")),
        "env_code": _resolve_var(section.get("env_code"), "default"),
        "spec_code": _resolve_var(section.get("spec_code"), ""),
        "git_repo": repo,
        "git_branch": branch,
        "git_commit": commit,
        "date": today,
        "seq": seq,
    }
    pattern = str(section.get("run_code_pattern", "{project}-{env}-{date}-{seq}"))
    context["run_code"] = pattern.format(
        project=context["project_code"], env=context["env_code"],
        date=today, seq=seq, branch=branch,
    )

    spec_path = next(iter(paths["requirements"].glob("*.json")), None)
    context["spec_version"] = ""
    if spec_path:
        try:
            context["spec_version"] = str((read_json(spec_path).get("meta") or {}).get("version") or "")
        except Exception:
            pass

    assets = _collect_assets(cfg, paths, context)
    registry = {
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "context": context,
        "assets": assets,
    }
    run_dir = resolve(str(cfg.get("records_dir", "records"))) / (run_id if run_id.startswith("run-") else f"run-{run_id}")
    write_json(run_dir / "asset-registry.json", registry)

    baselines = _collect_baseline(cfg, paths, context)
    write_json(run_dir / "case-baseline.json", {"run_id": run_id, "context": context, "baselines": baselines})

    print(f"资产台账: {len(assets)} 项，run_code={context['run_code']}")
    print(f"核心基线: {len(baselines)} 条")
    if args.sync_db:
        _sync_db(cfg, registry, baselines)


if __name__ == "__main__":
    main()
