#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa-toolkit（qa-case-pipeline）与 DSH 工作流的融合适配层。

复用 qa-toolkit 的能力：
- 用例生成：gen_cases.py（spec JSON → Markdown + Excel）
- 状态回填：fill_status.py（执行结果 → 状态列 + COUNTIF 统计 + 场景层聚合）
- 脚本位置：优先使用宿主注入的 DSH_QACASEPIPE_DIR（来自 ctx.qaCasepipe.skillDir），
  其次按配置与常见安装位置自动探测。

同时把 qa-toolkit 的三层用例（场景/接口/UI）转换成 DSH 工作流的 cases/*.yaml：
- 场景层 → cases/functional/*.yaml（步骤与预期结果文本化）
- 接口层 → cases/api/*.yaml（可直接被 tests/api 执行器执行）
- UI 层  → cases/ui/*.yaml（保守翻译 Playwright 片段为 EGO DSL，无法翻译的条目会明确提示）
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import WORKFLOW_ROOT, resolve, read_json, write_json  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None

SHEET_SCENARIO = "核心链路用例"
SHEET_API = "API接口用例"
SHEET_UI = "UI用例(Playwright)"
STATUS_PASS = "通过"
STATUS_PARTIAL = "部分通过"
STATUS_BLOCKED = "阻塞"
STATUS_SKIPPED = "未执行"
STATUS_FAIL = "失败"

# DSH 执行状态 → qa-case-pipeline 状态词表
STATUS_BY_RESULT = {
    "passed": STATUS_PASS,
    "failed": STATUS_FAIL,
    "errors": STATUS_BLOCKED,
    "skipped": STATUS_SKIPPED,
}

# 场景层聚合优先级：失败 > 部分通过 > 阻塞 > 通过 > 未执行
SCENARIO_PRIORITY = (STATUS_FAIL, STATUS_PARTIAL, STATUS_BLOCKED, STATUS_PASS, STATUS_SKIPPED)

CASE_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9]+-[A-Za-z0-9]+")


# ---------------------------------------------------------------- 配置

def casepipe_config(providers: dict) -> dict:
    """读取 providers.yaml → qa_casepipe 段并补齐默认值。"""
    pipe = dict((providers or {}).get("qa_casepipe") or {})
    outputs = pipe.get("outputs") or {}
    sheets = pipe.get("sheets") or {}
    return {
        "enabled": bool(pipe.get("enabled")),
        "spec": str(pipe.get("spec") or ""),
        "scripts_dir": str(pipe.get("scripts_dir") or ""),
        "md": str(outputs.get("md") or "reports/qa-cases.md"),
        "xlsx": str(outputs.get("xlsx") or "reports/qa-cases.xlsx"),
        "sheets": {
            "scenario": str(sheets.get("scenario") or SHEET_SCENARIO),
            "api": str(sheets.get("api") or SHEET_API),
            "ui": str(sheets.get("ui") or SHEET_UI),
        },
    }


# ---------------------------------------------------------------- 脚本定位

def find_scripts_dir(prefer: str = "") -> Path | None:
    """定位 qa-case-pipeline 的 scripts 目录。"""
    candidates: list[Path] = []
    if prefer:
        candidates.append(Path(prefer))
    env = os.environ.get("DSH_QACASEPIPE_DIR")
    if env:
        candidates.append(Path(env))
    home = Path.home()
    candidates.append(WORKFLOW_ROOT / "node_modules" / "dsh-qa-casepipe" / "assets" / "qa-case-pipeline" / "scripts")
    candidates.append(WORKFLOW_ROOT.parent / "qa-toolkit" / "skill" / "qa-case-pipeline" / "scripts")
    candidates.append(home / ".dsh" / "profiles" / "web" / "node_modules" / "dsh-qa-casepipe"
                      / "assets" / "qa-case-pipeline" / "scripts")
    profiles = home / ".dsh" / "profiles"
    if profiles.exists():
        for profile in profiles.iterdir():
            candidates.append(profile / "node_modules" / "dsh-qa-casepipe"
                              / "assets" / "qa-case-pipeline" / "scripts")
    for path in candidates:
        if path and (path / "gen_cases.py").exists():
            return path
    return None


# ---------------------------------------------------------------- 生成用例

def load_spec(spec_path: Path) -> dict:
    """读取 qa-case-pipeline spec。"""
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    for key in ("scenarios", "api", "ui"):
        spec.setdefault(key, [])
    return spec


def _parse_headers(text: str) -> dict:
    headers: dict[str, str] = {}
    for line in str(text or "").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            if key:
                headers[key] = value.strip()
    return headers


def _substitute(value: str) -> str:
    text = str(value or "")
    token = os.environ.get("DSH_API_TOKEN", "")
    client_id = os.environ.get("DSH_API_CLIENT_ID", "dsh-workflow")
    text = text.replace("{{token}}", token).replace("{{clientId}}", client_id)
    text = text.replace("{{base}}", os.environ.get("DSH_API_BASE_URL", ""))
    return text


def _api_path(url: str, base: str) -> str:
    text = _substitute(url)
    text = text.replace("{base}", "").replace("${base}", "")
    if base and text.startswith(base):
        text = text[len(base):]
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return text.lstrip("/") if text.startswith("/") else text


def _body_value(raw):
    if raw in (None, ""):
        return None
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if text in ("无请求体", "无", "-"):
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _api_case(item: dict, base: str, run_id: str) -> dict:
    assertion = item.get("assert") or {}
    expect: dict = {}
    if assertion.get("status"):
        expect["status_code"] = assertion["status"]
    if assertion.get("code") is not None:
        expect["json_path"] = "$.code"
        expect["equals"] = assertion["code"]
    if item.get("expect"):
        expect["note"] = str(item["expect"])[:300]
    request = {
        "method": str(item.get("method") or "GET").upper(),
        "path": _api_path(item.get("url", "/"), base),
        "headers": {k: _substitute(v) for k, v in _parse_headers(item.get("headers")).items()},
    }
    body = _body_value(item.get("body"))
    if body is not None:
        request["body"] = body
    return {
        "case_id": item.get("id"),
        "run_id": run_id,
        "title": item.get("name") or item.get("id"),
        "module": item.get("module") or "未分类",
        "priority": item.get("priority") or "P1",
        "related_func": item.get("related") or "",
        "request": request,
        "expect": expect,
        "source": "qa-case-pipeline",
    }


# ---------------------------------------------------------------- UI 翻译

def translate_playwright_steps(code: str, base: str) -> tuple[list, list]:
    """保守翻译 Playwright 片段为 EGO DSL；无法翻译的记入 notes。"""
    steps: list = []
    notes: list = []
    text = str(code or "")
    for placeholder in ("${BASE}", "${base}", "{BASE}", "{base}"):
        text = text.replace(placeholder, base)
    # 只剥离真正的行注释：// 前不是冒号（避免误删 https:// 里的双斜杠）
    text = re.sub(r"(?<!:)//.*$", "", text, flags=re.M)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    def push(action: str) -> None:
        steps.append({"action": action})

    for line in lines:
        m = re.search(r"page\.goto\(\s*[`'\"]([^`'\"]+)[`'\"]", line)
        if m:
            push(f"goto {_api_path(m.group(1), base)}")
            continue
        m = re.search(r"getByTestId\(\s*[`'\"]([^`'\"]+)[`'\"]\s*\)\s*\)\s*\.toBeVisible", line)
        if m:
            push(f'expect_visible loc=css:[data-testid={m.group(1)}]')
            continue
        m = re.search(r"getByTestId\(\s*[`'\"]([^`'\"]+)[`'\"]\s*\)\s*\)\s*\.click", line)
        if m:
            push(f'click loc=css:[data-testid={m.group(1)}]')
            continue
        m = re.search(r"getByRole\(\s*[`'\"]([^`'\"]+)[`'\"]\s*,\s*\{[^}]*name:\s*[`'\"]([^`'\"]+)[`'\"]", line)
        if m:
            push(f'click loc=role:{m.group(1)}[name="{m.group(2)}"]')
            continue
        m = re.search(r"getByText\(\s*[`'\"]([^`'\"]+)[`'\"]\s*\)\s*\)\s*\.click", line)
        if m:
            push(f'click text={m.group(1)}')
            continue
        m = re.search(r"page\.fill\(\s*[`'\"]([^`'\"]+)[`'\"]\s*,\s*[`'\"]([^`'\"]*)[`'\"]", line)
        if m:
            push(f"fill {m.group(1)} {m.group(2)}")
            continue
        m = re.search(r"toHaveText\(\s*[`'\"]([^`'\"]+)[`'\"]", line)
        if m:
            push(f'expect_text body {m.group(1)}')
            continue
        m = re.search(r"toHaveURL\((.*)\)", line)
        if m:
            notes.append(f"跳过无法翻译的 URL 断言: {m.group(1)[:80]}")
            continue
        if re.search(r"\.(count|innerText|textContent|inputValue)\(", line):
            notes.append(f"跳过取值类断言: {line[:80]}")
            continue
    return steps, notes


def _ui_case(item: dict, base: str, run_id: str) -> tuple[dict | None, list]:
    steps, notes = translate_playwright_steps(item.get("steps_playwright", ""), base)
    if not steps:
        notes.append("没有可翻译为 EGO DSL 的步骤，整条用例未落盘")
        return None, notes
    case = {
        "case_id": item.get("id"),
        "run_id": run_id,
        "title": item.get("name") or item.get("id"),
        "module": item.get("module") or "未分类",
        "priority": item.get("priority") or "P1",
        "related_func": item.get("related") or "",
        "steps": steps,
        "expect": item.get("expected") or "",
        "source": "qa-case-pipeline",
    }
    if notes:
        # 跳过的断言会随用例落盘，便于在报告/明细里看到人工补充点
        case["notes"] = notes
    return case, notes


# ---------------------------------------------------------------- 转换入口

def spec_to_cases(spec_path: Path, functional_dir: Path, api_dir: Path, ui_dir: Path,
                  run_id: str) -> dict:
    """把 qa-case-pipeline spec 转成 DSH 用例 YAML。"""
    if yaml is None:
        raise SystemExit("缺少依赖 PyYAML，请先执行: pip install PyYAML")
    spec = load_spec(spec_path)
    meta = spec.get("meta") or {}
    base = str(meta.get("base") or "")
    stats = {"functional": 0, "api": 0, "ui": 0, "notes": []}

    functional_dir.mkdir(parents=True, exist_ok=True)
    api_dir.mkdir(parents=True, exist_ok=True)
    ui_dir.mkdir(parents=True, exist_ok=True)

    scenarios = spec.get("scenarios") or []
    if scenarios:
        items = []
        for sc in scenarios:
            related = sc.get("related") or []
            if isinstance(related, list):
                related = ",".join(str(x) for x in related)
            items.append({
                "case_id": sc.get("id"),
                "run_id": run_id,
                "title": sc.get("name") or sc.get("id"),
                "module": sc.get("chain") or "未分类",
                "priority": sc.get("priority") or "P1",
                "precondition": sc.get("precondition") or "",
                "steps": [{
                    "step": 1,
                    "action": str(sc.get("steps") or ""),
                    "expected": str(sc.get("expected") or ""),
                }],
                "expect": str(sc.get("expected") or ""),
                "related_req": related,
                "source": "qa-case-pipeline",
            })
        path = functional_dir / "qa-pipeline.functional.yaml"
        path.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")
        stats["functional"] = len(items)

    api_items = spec.get("api") or []
    if api_items:
        items = [_api_case(item, base, run_id) for item in api_items if item.get("id")]
        path = api_dir / "qa-pipeline.api.yaml"
        path.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")
        stats["api"] = len(items)

    ui_items = spec.get("ui") or []
    if ui_items:
        items = []
        for item in ui_items:
            if not item.get("id"):
                continue
            case, notes = _ui_case(item, base, run_id)
            if case:
                items.append(case)
            stats["notes"].extend(notes)
        if items:
            path = ui_dir / "qa-pipeline.ui.yaml"
            path.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")
            stats["ui"] = len(items)

    return stats


# ---------------------------------------------------------------- 状态回填

def results_for_casepipe(details: list) -> list:
    """把 DSH 执行明细转成 qa-case-pipeline 的结果格式。"""
    results = []
    for item in details or []:
        status = STATUS_BY_RESULT.get(item.get("status"), STATUS_SKIPPED)
        entry = {
            "id": item.get("case_id"),
            "status": status,
            "detail": (item.get("message") or "")[:300],
        }
        response = item.get("resp_content") or ""
        m = re.search(r'"status_code":\s*(\d+)', str(response))
        if m:
            entry["httpStatus"] = int(m.group(1))
        m = re.search(r'"code":\s*(-?\d+)', str(response))
        if m:
            entry["code"] = int(m.group(1))
        results.append(entry)
    return results


def write_results(details: list, path: Path, env: str = "") -> Path:
    """写出 qa-case-pipeline 格式的结果文件。"""
    write_json(Path(path), {"results": results_for_casepipe(details), "base": env})
    return Path(path)


def fill_status(xlsx: Path, results_path: Path, sheet: str, scenario_sheet: str = "",
                env: str = "") -> dict:
    """调用 qa-toolkit 的 fill_status.py 回填执行状态。"""
    scripts = find_scripts_dir()
    if not scripts:
        return {"ok": False, "error": "未找到 qa-case-pipeline scripts 目录"}
    cmd = [sys.executable, str(scripts / "fill_status.py"), "--xlsx", str(xlsx),
           "--results", str(results_path), "--sheet", sheet]
    if scenario_sheet:
        cmd += ["--scenario-sheet", scenario_sheet]
    if env:
        cmd += ["--env", env]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return {
        "ok": proc.returncode == 0,
        "sheet": sheet,
        "stdout": (proc.stdout or "")[-1000:],
        "stderr": (proc.stderr or "")[-1000:],
    }


def _sheet_names(xlsx: Path) -> list:
    """读取工作簿的工作表名（openpyxl 缺失时返回空列表）。"""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return []
    try:
        wb = load_workbook(xlsx, read_only=True)
        names = list(wb.sheetnames)
        wb.close()
        return names
    except Exception:
        return []


def _ensure_scenario_headers(xlsx: Path, sheet_name: str) -> dict:
    """给场景层工作表补「执行状态」「实际结果」表头。

    不同版本的 qa-case-pipeline scripts/fill_status.py 行为不一致：
    新版会自己补表头，旧版只写数据不留表头，导致 Excel 里出现两列无名数据、
    COUNTIF 统计与筛选失效。这里在回填前统一补齐，与外部脚本版本无关。
    """
    try:
        from openpyxl import load_workbook
        from openpyxl.styles import Font
    except ImportError:
        return {"ok": False, "error": "缺少 openpyxl"}
    try:
        wb = load_workbook(xlsx)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"打开 Excel 失败: {exc}"}
    if sheet_name not in wb.sheetnames:
        return {"ok": False, "error": f"工作表不存在: {sheet_name}"}
    ws = wb[sheet_name]
    field_index = {ws.cell(row=1, column=c).value: c for c in range(1, ws.max_column + 1)}
    added: list[str] = []
    for col, title in ((field_index.get("执行状态") or 8, "执行状态"),
                       (field_index.get("实际结果") or 9, "实际结果")):
        if ws.cell(row=1, column=col).value is None:
            cell = ws.cell(row=1, column=col, value=title)
            cell.font = Font(color="FFFFFF", bold=True)
            added.append(title)
    if added:
        wb.save(xlsx)
    return {"ok": True, "added": added}


def fill_back(xlsx: Path, details: list, run_dir: Path, env: str = "",
              sheets: dict | None = None) -> dict:
    """把执行明细回填进 qa-toolkit 的 Excel（接口层 + UI 层 + 场景层聚合）。

    调用顺序：先回填 UI 层，再回填接口层并同时聚合场景层，
    保证场景层聚合读到的是两张表都已写入的状态。
    """
    sheets = sheets or {"scenario": SHEET_SCENARIO, "api": SHEET_API, "ui": SHEET_UI}
    xlsx = Path(xlsx)
    if not xlsx.exists():
        return {"ok": False, "error": f"Excel 不存在: {xlsx}"}
    scripts = find_scripts_dir()
    if not scripts:
        return {"ok": False, "error": "未找到 qa-case-pipeline scripts 目录"}

    results_path = Path(run_dir) / "qa-casepipe-results.json"
    write_results(details, results_path, env)

    names = _sheet_names(xlsx)
    api_sheet = sheets.get("api") or SHEET_API
    ui_sheet = sheets.get("ui") or SHEET_UI
    scenario_sheet = sheets.get("scenario") or SHEET_SCENARIO

    out = {"ok": True, "xlsx": str(xlsx), "results_file": str(results_path), "steps": []}
    if names:
        if api_sheet not in names and ui_sheet not in names:
            return {"ok": False, "error": f"Excel 中找不到 {api_sheet} / {ui_sheet} 工作表"}
    else:
        names = []

    # ① 场景层表头先补齐（兼容旧版 fill_status.py）
    out["headers"] = _ensure_scenario_headers(xlsx, scenario_sheet)
    # ② UI 层先回填（不带场景聚合）
    if not names or ui_sheet in names:
        out["steps"].append(fill_status(xlsx, results_path, ui_sheet, env=env))
    # ③ 接口层回填 + 场景层聚合
    if not names or api_sheet in names:
        out["steps"].append(fill_status(xlsx, results_path, api_sheet, scenario_sheet, env=env))
    elif out["steps"]:
        out["steps"].append(fill_status(xlsx, results_path, ui_sheet, scenario_sheet, env=env))

    out["ok"] = all(step.get("ok") for step in out["steps"])
    errors = [step.get("stderr") for step in out["steps"] if not step.get("ok")]
    if errors:
        out["error"] = "; ".join(str(e).strip() for e in errors if e)[:500]
    return out


# ---------------------------------------------------------------- 场景层聚合

def _related_ids(value) -> list:
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return CASE_ID_RE.findall(str(value or ""))


def _derive_scenario_status(statuses: list) -> tuple[str, str]:
    if not statuses:
        return STATUS_SKIPPED, "无关联 API/UI 用例执行结果"
    for status in SCENARIO_PRIORITY:
        if status in statuses:
            if status == STATUS_FAIL:
                return status, f"存在失败项 {statuses.count(STATUS_FAIL)} 条"
            if status == STATUS_PARTIAL:
                return status, f"含部分通过项 {statuses.count(STATUS_PARTIAL)} 条"
            if status == STATUS_BLOCKED:
                return status, f"含阻塞项 {statuses.count(STATUS_BLOCKED)} 条"
            return status, f"关联用例全部通过（{statuses.count(STATUS_PASS)} 条）"
    return STATUS_SKIPPED, "关联用例均未执行"


def aggregate_scenarios(spec: dict, details: list) -> list:
    """按 spec 的 related 关系把 API/UI 执行状态聚合到场景层。"""
    status_by_id = {}
    for item in details or []:
        status_by_id[item.get("case_id")] = STATUS_BY_RESULT.get(item.get("status"), STATUS_SKIPPED)
    rows = []
    for sc in (spec or {}).get("scenarios") or []:
        related = _related_ids(sc.get("related"))
        statuses = [status_by_id[cid] for cid in related if cid in status_by_id]
        derived, summary = _derive_scenario_status(statuses)
        rows.append({
            "id": sc.get("id"),
            "chain": sc.get("chain") or "",
            "name": sc.get("name") or sc.get("id"),
            "priority": sc.get("priority") or "",
            "status": derived,
            "detail": summary,
            "related": related,
        })
    return rows


def scenario_stats(rows: list) -> dict:
    """场景层状态统计。"""
    stats = {status: 0 for status in SCENARIO_PRIORITY}
    for row in rows or []:
        status = row.get("status") or STATUS_SKIPPED
        stats[status] = stats.get(status, 0) + 1
    return stats


# ---------------------------------------------------------------- 产物生成

def generate(spec: Path, out_md: Path, out_xlsx: Path | None = None,
             scripts_dir: str = "") -> dict:
    """调用 qa-toolkit 的 gen_cases.py 生成 Markdown / Excel。"""
    scripts = find_scripts_dir(scripts_dir)
    if not scripts:
        return {"ok": False, "error": "未找到 qa-case-pipeline scripts 目录"}
    cmd = [sys.executable, str(scripts / "gen_cases.py"), "--spec", str(spec), "--out-md", str(out_md)]
    if out_xlsx:
        out_xlsx.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["--out-xlsx", str(out_xlsx)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return {
        "ok": proc.returncode == 0,
        "stdout": (proc.stdout or "")[-1000:],
        "stderr": (proc.stderr or "")[-1000:],
    }


# ---------------------------------------------------------------- qa-toolkit 其余能力
# 已接入：gen_cases.py / fill_status.py
# 本段接入：login.mjs / api_runner.js / qa_review.py / ui_spec_template.ts / references

def _run_tool(cmd: list, timeout: int = 180, env: dict | None = None) -> dict:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=str(WORKFLOW_ROOT))
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[-2000:], "stderr": (proc.stderr or "")[-2000:]}
    except FileNotFoundError as exc:
        return {"ok": False, "error": f"命令不存在: {exc}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时（{timeout}s）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def qa_login(config: Path, scripts_dir: str = "", headed: bool = False,
             show: bool = False, env_extra: dict | None = None) -> dict:
    """调用 qa-toolkit 的 login.mjs：Playwright 登录并产出 storageState。"""
    scripts = find_scripts_dir(scripts_dir)
    if not scripts:
        return {"ok": False, "error": "未找到 qa-case-pipeline scripts 目录"}
    if not Path(config).exists():
        return {"ok": False, "error": f"登录配置不存在: {config}"}
    cmd = ["node", str(scripts / "login.mjs"), "--config", str(Path(config).resolve())]
    if headed:
        cmd.append("--headed")
    if show:
        cmd.append("--show")
    env = os.environ.copy()
    env.update(env_extra or {})
    result = _run_tool(cmd, timeout=180, env=env)
    result["script"] = "login.mjs"
    return result


def _node_script_path(script: Path) -> tuple[Path, Path | None]:
    """处理 qa-toolkit 上游 api_runner.js 的 ESM/CommonJS 冲突。

    插件包声明 `"type": "module"`，但脚本内部使用 `require()`。
    检测到这种组合时，复制为临时 `.cjs` 文件执行（脚本不依赖 __dirname）。
    返回 (实际执行路径, 需要清理的临时路径)。
    """
    try:
        source = script.read_text(encoding="utf-8")
    except OSError:
        return script, None
    if "require(" not in source:
        return script, None
    parents = list(script.parents)[:6]
    for parent in parents:
        pkg = parent / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                break
            if str(data.get("type") or "").lower() == "module":
                import tempfile

                tmp = Path(tempfile.gettempdir()) / f"dsh-{script.stem}-{os.getpid()}.cjs"
                tmp.write_text(source, encoding="utf-8")
                return tmp, tmp
            break
    return script, None


def qa_api_run(plan: Path, out: Path, config: Path | None = None,
               scripts_dir: str = "", allow_write: bool = False,
               env_extra: dict | None = None) -> dict:
    """调用 qa-toolkit 的 api_runner.js：真实 API 批量执行 + 断言 + 结果 JSON。"""
    scripts = find_scripts_dir(scripts_dir)
    if not scripts:
        return {"ok": False, "error": "未找到 qa-case-pipeline scripts 目录"}
    if not Path(plan).exists():
        return {"ok": False, "error": f"API 计划不存在: {plan}"}
    runner, tmp = _node_script_path(scripts / "api_runner.js")
    cmd = ["node", str(runner), "--plan", str(Path(plan).resolve()),
           "--out", str(Path(out).resolve())]
    if config and Path(config).exists():
        cmd += ["--config", str(Path(config).resolve())]
    if allow_write:
        cmd.append("--allow-write")
    env = os.environ.copy()
    env.update(env_extra or {})
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = _run_tool(cmd, timeout=300, env=env)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
    result.update({"script": "api_runner.js", "out": str(out),
                   "results": _read_json_safe(out)})
    return result


def qa_review(xlsx: Path, results: list, out: Path, evidence_root: Path | None = None,
              scripts_dir: str = "", strict: bool = False) -> dict:
    """调用 qa-toolkit 的 qa_review.py：产物审核（错误/告警/P0 占比/模拟透明度）。"""
    scripts = find_scripts_dir(scripts_dir)
    if not scripts:
        return {"ok": False, "error": "未找到 qa-case-pipeline scripts 目录"}
    if not Path(xlsx).exists():
        return {"ok": False, "error": f"用例 Excel 不存在: {xlsx}"}
    cmd = [sys.executable, str(scripts / "qa_review.py"), "--xlsx", str(Path(xlsx).resolve())]
    for item in results or []:
        p = Path(item)
        if p.exists():
            cmd += ["--results", str(p.resolve())]
    if evidence_root:
        cmd += ["--evidence-root", str(Path(evidence_root).resolve())]
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd += ["--out", str(out.resolve())]
    if strict:
        cmd.append("--strict")
    result = _run_tool(cmd, timeout=180)
    result.update({"script": "qa_review.py", "out": str(out),
                   "report": out.read_text(encoding="utf-8")[:4000] if out.exists() else ""})
    return result


def qa_playwright_template(dest: Path, scripts_dir: str = "") -> dict:
    """复制 qa-toolkit 的 ui_spec_template.ts 作为真实环境 Playwright 测试模板。"""
    scripts = find_scripts_dir(scripts_dir)
    if not scripts:
        return {"ok": False, "error": "未找到 qa-case-pipeline scripts 目录"}
    src = scripts / "ui_spec_template.ts"
    if not src.exists():
        return {"ok": False, "error": f"模板不存在: {src}"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return {"ok": True, "script": "ui_spec_template.ts", "out": str(dest)}


def qa_copy_references(dest_dir: Path, scripts_dir: str = "") -> dict:
    """把 qa-toolkit 的两篇规范复制进批次产物，供追溯。"""
    scripts = find_scripts_dir(scripts_dir)
    if not scripts:
        return {"ok": False, "error": "未找到 qa-case-pipeline scripts 目录"}
    ref_src = scripts.parent / "references"
    copied = []
    if ref_src.is_dir():
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in ("excel-columns.md", "project-config.md"):
            src = ref_src / name
            if src.exists():
                (dest_dir / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                copied.append(name)
    return {"ok": True, "files": copied, "dir": str(dest_dir)}


def _read_json_safe(path: Path) -> dict | list | None:
    try:
        return read_json(Path(path))
    except Exception:  # noqa: BLE001
        return None


def extended_run(run_dir: Path, pipe: dict, providers: dict | None = None) -> dict:
    """按顺序跑通 qa-toolkit 扩展能力：登录 → API 执行 → 模板/规范 → 产物审核。

    每项独立捕获异常，任一失败不影响其它项，也不阻断主流水线。
    """
    out = {"enabled": False, "steps": []}
    ext = ((providers or {}).get("qa_casepipe") or {}).get("extended") or {}
    if not ext.get("enabled"):
        return out
    out["enabled"] = True
    scripts_dir = str(pipe.get("scripts_dir") or "")

    def record(name: str, result: dict):
        out["steps"].append({"name": name, **result})

    if ext.get("login", {}).get("enabled"):
        cfg = resolve(ext["login"].get("config") or "")
        record("login", qa_login(cfg, scripts_dir,
                                 headed=bool(ext["login"].get("headed")),
                                 show=bool(ext["login"].get("show")),
                                 env_extra=ext["login"].get("env") or {}))

    api = ext.get("api") or {}
    if api.get("enabled"):
        plan = resolve(api.get("plan") or "")
        dest = resolve(api.get("out") or f"records/{run_dir.name}/qa-api-results.json")
        cfg = resolve(api.get("config") or "") if api.get("config") else None
        record("api_runner", qa_api_run(plan, dest, cfg, scripts_dir,
                                        allow_write=bool(api.get("allow_write")),
                                        env_extra=api.get("env") or {}))

    if ext.get("playwright_template"):
        record("playwright_template",
               qa_playwright_template(run_dir / "qa-ui-spec.ts", scripts_dir))

    if ext.get("copy_references"):
        record("references", qa_copy_references(run_dir / "qa-references", scripts_dir))

    review = ext.get("review") or {}
    if review.get("enabled"):
        xlsx = resolve(str(review.get("xlsx") or pipe.get("xlsx") or ""))
        results = [p for p in (review.get("results") or []) if p]
        dest = resolve(review.get("out") or f"reports/qa-review-{run_dir.name}.md")
        record("qa_review", qa_review(xlsx, [resolve(p) for p in results], dest,
                                      scripts_dir=scripts_dir,
                                      strict=bool(review.get("strict"))))

    out["ok"] = all(step.get("ok", True) for step in out["steps"])
    return out


if __name__ == "__main__":
    print(json.dumps({"scripts_dir": str(find_scripts_dir() or "")}, ensure_ascii=False))
