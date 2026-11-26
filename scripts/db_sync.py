#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 过程数据 MySQL 回流。

用例 / 批次 / 单用例结果（含请求响应、断言、截图）/ 报告（含 JSON 内容）四类数据统一回流。
表结构与列名通过 config/workflow.yaml 的 db.columns 映射适配，脚本无需改动。

用法:
  python3 scripts/db_sync.py --dry-run
  python3 scripts/db_sync.py
  python3 scripts/db_sync.py --run run-20261126-153000
  python3 scripts/db_sync.py --suggest-ddl
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import load_config, ensure_dirs, read_json, resolve, WORKFLOW_ROOT  # noqa: E402

try:
    import yaml
except ImportError:
    print("缺少依赖 PyYAML: pip install PyYAML", file=sys.stderr)
    raise

try:
    import pymysql
except ImportError:
    pymysql = None

DEFAULT_COLUMN_MAP = {
    "case_table": {
        "run_id": "run_id", "case_id": "case_id", "case_type": "case_type",
        "title": "title", "module": "module", "priority": "priority",
        "precondition": "precondition", "related_req": "related_req", "related_func": "related_func",
        "steps_json": "steps_json", "request_json": "request_json", "assert_json": "assert_json",
        "created_at": "created_at",
    },
    "run_table": {
        "run_id": "run_id", "run_name": "run_name", "demand_hash": "demand_hash",
        "workspace_hash": "workspace_hash", "start_time": "start_time", "end_time": "end_time",
        "total": "total", "passed": "passed", "failed": "failed", "errors": "errors",
        "skipped": "skipped", "pass_rate": "pass_rate", "duration_sec": "duration_sec",
        "status": "status", "report_path": "report_path",
    },
    "case_result_table": {
        "run_id": "run_id", "case_id": "case_id", "case_type": "case_type", "attempt": "attempt",
        "status": "status", "start_time": "start_time", "end_time": "end_time", "cost_ms": "cost_ms",
        "message": "message", "req_content": "req_content", "resp_content": "resp_content",
        "assert_detail": "assert_detail", "screenshot_path": "screenshot_path",
    },
    "report_table": {
        "run_id": "run_id", "total": "total", "passed": "passed", "failed": "failed",
        "skipped": "skipped", "pass_rate": "pass_rate", "report_md_path": "report_md_path",
        "report_json": "report_json", "report_html_path": "report_html_path",
        "conclusion": "conclusion", "created_at": "created_at",
    },
}

SUGGEST_DDL = """
-- 建议表结构（脚本不会自动建表；拿到真实 DDL 后只改 workflow.yaml -> db.columns）
CREATE TABLE IF NOT EXISTS dsh_test_runs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id VARCHAR(64) NOT NULL UNIQUE,
  run_name VARCHAR(255),
  demand_hash VARCHAR(64),
  workspace_hash VARCHAR(64),
  start_time DATETIME,
  end_time DATETIME,
  total INT DEFAULT 0,
  passed INT DEFAULT 0,
  failed INT DEFAULT 0,
  errors INT DEFAULT 0,
  skipped INT DEFAULT 0,
  pass_rate DECIMAL(5,2),
  duration_sec DECIMAL(10,2),
  status TINYINT COMMENT '0运行中 1完成 2中断',
  report_path VARCHAR(512),
  ext_json JSON,
  KEY idx_start_time (start_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='测试批次表';

CREATE TABLE IF NOT EXISTS dsh_test_cases (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id VARCHAR(64) NOT NULL,
  case_id VARCHAR(64) NOT NULL,
  case_type VARCHAR(16) NOT NULL COMMENT 'functional/api/ui',
  title VARCHAR(512),
  module VARCHAR(128),
  priority VARCHAR(8),
  precondition TEXT,
  related_req VARCHAR(64),
  related_func VARCHAR(64),
  steps_json JSON,
  request_json JSON,
  assert_json JSON,
  created_at DATETIME,
  ext_json JSON,
  UNIQUE KEY uk_run_case (run_id, case_id),
  KEY idx_run_id (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用例表';

CREATE TABLE IF NOT EXISTS dsh_case_results (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id VARCHAR(64) NOT NULL,
  case_id VARCHAR(64) NOT NULL,
  case_type VARCHAR(16),
  attempt INT DEFAULT 1,
  status VARCHAR(16) COMMENT 'passed/failed/errors/skipped',
  start_time DATETIME,
  end_time DATETIME,
  cost_ms INT,
  message TEXT,
  req_content LONGTEXT,
  resp_content LONGTEXT,
  assert_detail JSON,
  screenshot_path VARCHAR(512),
  ext_json JSON,
  UNIQUE KEY uk_run_case_attempt (run_id, case_id, attempt),
  KEY idx_run_id (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用例执行结果表';

CREATE TABLE IF NOT EXISTS dsh_test_reports (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id VARCHAR(64) NOT NULL UNIQUE,
  total INT,
  passed INT,
  failed INT,
  skipped INT,
  pass_rate DECIMAL(5,2),
  report_md_path VARCHAR(512),
  report_json LONGTEXT,
  report_html_path VARCHAR(512),
  conclusion VARCHAR(1024),
  created_at DATETIME,
  ext_json JSON
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='测试报告表';
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sql_datetime(value) -> str:
    if not value:
        return _now()
    text = str(value)
    for fmt in ("%Y%m%d-%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return _now()


def _json_text(value):
    """空值返回 None，避免把空串写入 MySQL JSON 列被拒绝。"""
    if value in (None, "", [], {}):
        return None
    return json.dumps(value, ensure_ascii=False)


def _dir_hash(path: Path) -> str:
    """基于文件名/大小/修改时间生成快照 hash，避免读取大文件正文。"""
    h = hashlib.sha256()
    if path.exists():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                st = p.stat()
                h.update(str(p.relative_to(path)).encode("utf-8"))
                h.update(str(st.st_size).encode("utf-8"))
                h.update(str(int(st.st_mtime)).encode("utf-8"))
    return h.hexdigest()


def load_db_env(env_file: Path) -> dict:
    out = {}
    if not env_file.exists():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def get_column_map(cfg: dict, table_key: str) -> dict:
    merged = dict(DEFAULT_COLUMN_MAP[table_key])
    override = ((cfg.get("db") or {}).get("columns") or {}).get(table_key) or {}
    for k, v in override.items():
        merged[k] = v
    return {k: v for k, v in merged.items() if v}


def parse_yaml_case(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    return data if isinstance(data, list) else [data]


def build_case_rows(cfg, paths, run_id: str) -> list:
    rows = []
    from dsh_common import collect_yaml_files
    for key, ctype in (("functional", "functional"), ("api_cases", "api"), ("ui_cases", "ui")):
        for path in collect_yaml_files(paths[key]):
            if path.name.startswith("example."):
                continue
            for item in parse_yaml_case(path):
                if not isinstance(item, dict) or not item.get("case_id"):
                    continue
                rows.append({
                    "run_id": item.get("run_id") or run_id,
                    "case_id": item.get("case_id"),
                    "case_type": ctype,
                    "title": item.get("title"),
                    "module": item.get("module"),
                    "priority": item.get("priority"),
                    "precondition": item.get("precondition"),
                    "related_req": item.get("related_req"),
                    "related_func": item.get("related_func"),
                    "steps_json": _json_text(item.get("steps") or []),
                    "request_json": _json_text(item.get("request") or {}) if ctype == "api" else None,
                    "assert_json": _json_text(item.get("expect") or item.get("assert_rule") or {}),
                    "created_at": _now(),
                })
    return rows


def _find_run(records_dir: Path, run_arg: str):
    latest = records_dir / "latest.json"
    if run_arg:
        name = run_arg if str(run_arg).startswith("run-") else f"run-{run_arg}"
        run_dir = records_dir / name
    elif latest.exists():
        run_dir = Path(read_json(latest)["run_dir"])
    else:
        return None, None, None
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        return None, None, None
    summary = read_json(summary_path)
    details = []
    # functional-detail.json 是场景层派生结果，与 api/ui 明细同构，一并回流
    for name in ("api-detail.json", "ui-detail.json", "functional-detail.json"):
        p = run_dir / name
        if p.exists():
            details.extend(read_json(p))
    return run_dir, summary, details


def _report_info(cfg, run_id: str):
    reports_dir = resolve(cfg["reports_dir"])
    md = reports_dir / f"report-{run_id}.md"
    js = reports_dir / f"report-{run_id}.json"
    html = reports_dir / f"report-{run_id}.html"
    report_json = js.read_text(encoding="utf-8") if js.exists() else ""
    return (str(md) if md.exists() else ""), report_json, (str(html) if html.exists() else "")


def _build_sql(table: str, row: dict, column_map: dict, unique_keys=()):
    pairs = [(column_map[k], v) for k, v in row.items() if k in column_map and v is not None]
    if not pairs:
        return None, []
    cols = [c for c, _ in pairs]
    vals = [v for _, v in pairs]
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    updates = ", ".join(f"{c} = VALUES({c})" for c in cols if c not in unique_keys)
    if updates:
        sql += f" ON DUPLICATE KEY UPDATE {updates}"
    return sql, vals


def _pass_rate(o: dict) -> float:
    total = o.get("total") or 0
    return round(o.get("passed", 0) / total * 100, 2) if total else 0.0


def main():
    parser = argparse.ArgumentParser(description="DSH 过程数据 MySQL 回流")
    parser.add_argument("--run", help="指定 run_id；缺省取最近一次")
    parser.add_argument("--dry-run", action="store_true", help="只打印 SQL，不写库")
    parser.add_argument("--suggest-ddl", action="store_true", help="打印默认映射的建议建表语句")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    db_cfg = cfg.get("db") or {}

    if args.suggest_ddl:
        print(SUGGEST_DDL)
        return

    if pymysql is None and not args.dry_run:
        print("缺少依赖 PyMySQL: pip install PyMySQL")
        raise SystemExit(1)

    env = load_db_env(WORKFLOW_ROOT / db_cfg.get("env_file", "config/mysql.env"))
    if not args.dry_run and not env.get("DSH_DB_NAME"):
        print("未找到 MySQL 连接配置，请复制 config/mysql.example.env 为 config/mysql.env 并填写")
        raise SystemExit(1)
    if not args.dry_run and not db_cfg.get("enabled"):
        print("db.enabled 当前为 false。确认连接与列映射后，把 config/workflow.yaml 中改为 true")
        raise SystemExit(1)

    run_dir, summary, details = _find_run(paths["records"], args.run)
    if summary is None:
        print("没有可回流的执行记录，请先运行 run_tests.py")
        raise SystemExit(1)
    run_id = summary["run_id"]
    o = summary["overall"]
    rate = _pass_rate(o)

    conn = None
    if not args.dry_run:
        conn = pymysql.connect(
            host=env.get("DSH_DB_HOST", "127.0.0.1"),
            port=int(env.get("DSH_DB_PORT", 3306)),
            user=env.get("DSH_DB_USER", "root"),
            password=env.get("DSH_DB_PASSWORD", ""),
            database=env.get("DSH_DB_NAME"),
            charset=env.get("DSH_DB_CHARSET", "utf8mb4"),
            autocommit=True,
        )

    tables = db_cfg.get("tables") or {}
    sqls = []

    case_map = get_column_map(cfg, "case_table")
    for row in build_case_rows(cfg, paths, run_id):
        sql, vals = _build_sql(tables.get("case_table", "dsh_test_cases"), row, case_map, ("run_id", "case_id"))
        if sql:
            sqls.append(("case", sql, vals))

    report_md, report_json, report_html = _report_info(cfg, run_id)
    start_time = _sql_datetime(summary.get("timestamp"))
    run_row = {
        "run_id": run_id,
        "run_name": cfg.get("workflow", {}).get("name", "dsh"),
        "demand_hash": _dir_hash(paths["requirements"]),
        "workspace_hash": _dir_hash(paths["source"]),
        "start_time": start_time,
        "end_time": _now(),
        "total": o.get("total"), "passed": o.get("passed"), "failed": o.get("failed"),
        "errors": o.get("errors"), "skipped": o.get("skipped"),
        "pass_rate": rate, "duration_sec": o.get("duration_sec"),
        "status": 2 if summary.get("exit_code") == 3 else 1,
        "report_path": report_md,
    }
    sql, vals = _build_sql(tables.get("run_table", "dsh_test_runs"), run_row,
                           get_column_map(cfg, "run_table"), ("run_id",))
    if sql:
        sqls.append(("run", sql, vals))

    res_map = get_column_map(cfg, "case_result_table")
    for it in details:
        try:
            cost_ms = int(float(it.get("time") or 0) * 1000)
        except (TypeError, ValueError):
            cost_ms = int(it.get("cost_ms") or 0)
        row = {
            "run_id": run_id,
            "case_id": it.get("case_id"),
            "case_type": it.get("suite") or ("api" if "api" in (it.get("classname") or "") else "ui"),
            "attempt": it.get("attempt") or 1,
            "status": it.get("status"),
            "start_time": it.get("start_time") or start_time,
            "end_time": it.get("end_time") or _now(),
            "cost_ms": cost_ms,
            "message": (it.get("message") or "")[:2000],
            "req_content": it.get("req_content") or "",
            "resp_content": it.get("resp_content") or "",
            "assert_detail": _json_text(it.get("assert_detail") or []),
            "screenshot_path": it.get("screenshot_path") or "",
        }
        sql, vals = _build_sql(tables.get("case_result_table", "dsh_case_results"), row, res_map,
                               ("run_id", "case_id", "attempt"))
        if sql:
            sqls.append(("result", sql, vals))

    rep_row = {
        "run_id": run_id, "total": o.get("total"), "passed": o.get("passed"),
        "failed": o.get("failed"), "skipped": o.get("skipped"), "pass_rate": rate,
        "report_md_path": report_md, "report_json": report_json, "report_html_path": report_html,
        "conclusion": ("全部通过" if o.get("failed", 0) + o.get("errors", 0) == 0
                       else f"存在 {o.get('failed', 0) + o.get('errors', 0)} 个失败用例，需排查"),
        "created_at": _now(),
    }
    sql, vals = _build_sql(tables.get("report_table", "dsh_test_reports"), rep_row,
                           get_column_map(cfg, "report_table"), ("run_id",))
    if sql:
        sqls.append(("report", sql, vals))

    counts = {"case": 0, "run": 0, "result": 0, "report": 0}
    for kind, sql, vals in sqls:
        if args.dry_run:
            preview = sql
            for v in vals:
                preview = preview.replace("%s", json.dumps(str(v), ensure_ascii=False), 1)
            print(f"[{kind}] {preview}")
        else:
            with conn.cursor() as cur:
                cur.execute(sql, vals)
        counts[kind] += 1

    print(f"回流完成: 用例 {counts['case']} 条 | 批次 {counts['run']} 条 | "
          f"结果 {counts['result']} 条 | 报告 {counts['report']} 条"
          + ("（dry-run 未实际写库）" if args.dry_run else ""))


if __name__ == "__main__":
    main()
