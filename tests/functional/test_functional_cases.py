# -*- coding: utf-8 -*-
"""功能用例数据驱动执行器。

functional 用例的 `steps[].action` / `expected` 是自然语言描述，无法直接机判。
本执行器读取用例上新增的 `checks` 字段（可执行断言列表），逐条打真实 HTTP
请求并机判；用例未声明 `checks` 时记为 skipped 并在报告里显式暴露，
绝不把「没有断言」当成「通过」。

checks 结构::

    checks:
      - name: 匿名上下文
        request: {method: GET, path: /api/auth/me}
        expect:  {status_code: 200, json_path: $.data.authMode, equals: none}
      - name: P95 分桶单测
        command: cd backend && .venv/bin/python -m pytest ../e2e/test_mirror_p95.py -q
        expect_exit_code: 0
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
import requests

try:
    import yaml
except ImportError:
    pytest.exit("缺少依赖 PyYAML，请执行: pip install PyYAML", returncode=1)

WORKFLOW_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKFLOW_ROOT / "scripts"))
from dsh_common import mask_sensitive, safe_case_id  # noqa: E402

CASE_DIR = WORKFLOW_ROOT / "cases" / "functional"
API_BASE_URL = os.environ.get("DSH_API_BASE_URL", "http://localhost:8000")
API_TIMEOUT = float(os.environ.get("DSH_API_TIMEOUT", "15"))
COMMAND_TIMEOUT = float(os.environ.get("DSH_COMMAND_TIMEOUT", "300"))
IGNORE_FILES = {"example.functional.yaml"}


def _collect_cases():
    cases = []
    if not CASE_DIR.exists():
        return cases
    for path in sorted(CASE_DIR.glob("*.y*ml")):
        if path.name in IGNORE_FILES:
            continue
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("case_id"):
                cases.append((item["case_id"], item))
    return cases


CASES = _collect_cases()
if not CASES:
    pytest.skip("cases/functional 下没有可用用例", allow_module_level=True)


def _now():
    return datetime.now().isoformat(timespec="milliseconds")


def _write_runtime(record: dict):
    run_dir = os.environ.get("DSH_RUN_DIR")
    if not run_dir:
        return
    runtime_dir = Path(run_dir) / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / f"functional-{safe_case_id(record['case_id'])}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _json_get(obj, json_path: str):
    """简易 JSONPath：支持 $.a.b.c 与数组下标 $.list[0].name。"""
    if not json_path.startswith("$."):
        raise ValueError(f"仅支持 $. 开头的 JSONPath: {json_path}")
    cur = obj
    for part in json_path[2:].split("."):
        token = part.strip()
        if not token:
            continue
        indexes = []
        name = token
        while "[" in name:
            idx = name.index("[")
            close = name.index("]")
            indexes.append(name[idx + 1:close])
            name = name[:idx] + name[close + 1:]
        if name:
            if not isinstance(cur, dict):
                raise KeyError(f"在非字典上取字段 {name!r}（{json_path}）")
            cur = cur.get(name)
        for i in indexes:
            if not isinstance(cur, list):
                raise KeyError(f"在非数组上取下标 {i}（{json_path}）")
            cur = cur[int(i)]
    return cur


def _request(req: dict):
    method = str(req.get("method", "GET")).upper()
    path = req.get("path", "/")
    url = API_BASE_URL.rstrip("/") + "/" + str(path).lstrip("/")
    params = req.get("params") or {}
    headers = req.get("headers") or {}
    body = req.get("body")
    if isinstance(body, (dict, list)):
        return requests.request(method, url, headers=headers, params=params,
                                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                timeout=API_TIMEOUT)
    return requests.request(method, url, headers=headers, params=params, data=body, timeout=API_TIMEOUT)


def _settle(expect: dict, resp):
    """按 expect 判定单个 check，返回 (ok, detail)。"""
    expected_status = expect.get("status_code", 200)
    if resp.status_code != expected_status:
        return False, f"状态码期望 {expected_status}，实际 {resp.status_code}，响应 {resp.text[:300]}"

    if expect.get("contains"):
        if str(expect["contains"]) not in resp.text:
            return False, f"响应不含 `{expect['contains']}`，实际 {resp.text[:300]}"

    if expect.get("json_path"):
        try:
            payload = resp.json()
        except ValueError:
            return False, f"响应不是合法 JSON: {resp.text[:300]}"
        try:
            actual = _json_get(payload, expect["json_path"])
        except (KeyError, IndexError, ValueError) as e:
            return False, f"JSONPath {expect['json_path']} 解析失败: {e}"
        if "equals" in expect and actual != expect["equals"]:
            return False, f"{expect['json_path']} 期望 {expect['equals']!r}，实际 {actual!r}"
    return True, "ok"


def _resolve_cwd(chk):
    """命令型 check 的工作目录：check.cwd > DSH_COMMAND_CWD > 含 backend 的上层目录。"""
    raw = chk.get("cwd") or os.environ.get("DSH_COMMAND_CWD")
    if raw:
        path = Path(raw)
        return (path if path.is_absolute() else (WORKFLOW_ROOT / path)).resolve()
    for candidate in (WORKFLOW_ROOT, WORKFLOW_ROOT.parent, WORKFLOW_ROOT.parent.parent):
        if (candidate / "backend").exists():
            return candidate
    return WORKFLOW_ROOT


def _settle_command(chk):
    """执行命令型 check，返回 (ok, detail)。"""
    command = str(chk.get("command") or "").strip()
    expected = int(chk.get("expect_exit_code", 0))
    if not command:
        return False, "command 型 check 缺少 command"
    cwd = _resolve_cwd(chk)
    try:
        proc = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True,
                              text=True, timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"命令超时（>{COMMAND_TIMEOUT:.0f}s）: {command[:120]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    tail = output[-500:]
    if proc.returncode != expected:
        return False, (f"退出码期望 {expected}，实际 {proc.returncode}（cwd={cwd}）\n"
                       f"输出片段: {tail}")
    return True, f"退出码 {proc.returncode} 符合预期（cwd={cwd}）"


@pytest.mark.parametrize("case_id,case", CASES, ids=[c[0] for c in CASES])
def test_functional_case(case_id, case):
    checks = case.get("checks") or []
    record = {
        "run_id": os.environ.get("DSH_RUN_ID", ""),
        "case_id": case_id,
        "title": case.get("title"),
        "status": "passed",
        "start_time": _now(),
        "assert_detail": [],
    }
    started = time.time()

    if not checks:
        record["status"] = "skipped"
        record["error_msg"] = "用例未声明可执行 checks，无法机判"
        record["end_time"] = _now()
        record["cost_ms"] = int((time.time() - started) * 1000)
        _write_runtime(record)
        pytest.skip(f"[{case_id}] 未声明可执行 checks，跳过")

    try:
        for idx, chk in enumerate(checks, 1):
            name = chk.get("name") or f"check-{idx}"
            try:
                if chk.get("command"):
                    ok, detail = _settle_command(chk)
                    request_info = {"command": chk.get("command"),
                                    "expect_exit_code": chk.get("expect_exit_code", 0),
                                    "cwd": str(_resolve_cwd(chk))}
                    expect_info = {"expect_exit_code": chk.get("expect_exit_code", 0)}
                else:
                    resp = _request(chk.get("request") or {})
                    ok, detail = _settle(chk.get("expect") or {}, resp)
                    request_info = chk.get("request") or {}
                    expect_info = chk.get("expect") or {}
            except Exception as e:  # 网络异常等，记为失败而非崩溃
                ok, detail = False, f"{type(e).__name__}: {e}"
                request_info = chk.get("request") or chk.get("command") or {}
                expect_info = chk.get("expect") or {}
            record["assert_detail"].append({
                "name": name,
                "passed": bool(ok),
                "detail": mask_sensitive(detail),
                "request": mask_sensitive(request_info),
                "expect": mask_sensitive(expect_info),
            })
            assert ok, f"[{case_id}] {name} 失败: {detail}"
    except BaseException as e:
        record["status"] = "failed"
        record["error_msg"] = str(e)[:2000]
        raise
    finally:
        record["end_time"] = _now()
        record["cost_ms"] = int((time.time() - started) * 1000)
        _write_runtime(record)