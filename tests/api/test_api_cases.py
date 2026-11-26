# -*- coding: utf-8 -*-
"""API 用例数据驱动执行器（含请求/响应/断言明细采集）。"""
import json
import os
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

CASE_DIR = WORKFLOW_ROOT / "cases" / "api"
API_BASE_URL = os.environ.get("DSH_API_BASE_URL", "http://localhost:8000")
API_TIMEOUT = float(os.environ.get("DSH_API_TIMEOUT", "15"))
IGNORE_FILES = {"example.api.yaml"}


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
    pytest.skip("cases/api 下没有可用用例", allow_module_level=True)


def _now():
    return datetime.now().isoformat(timespec="milliseconds")


def _write_runtime(record: dict):
    run_dir = os.environ.get("DSH_RUN_DIR")
    if not run_dir:
        return
    runtime_dir = Path(run_dir) / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / f"api-{safe_case_id(record['case_id'])}.json"
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
        m = []
        name = token
        while "[" in name:
            idx = name.index("[")
            close = name.index("]")
            m.append(name[idx + 1:close])
            name = name[:idx] + name[close + 1:]
        if name:
            if not isinstance(cur, dict):
                raise AssertionError(f"JSONPath `{json_path}` 取值失败: {name} 处不是对象")
            cur = cur[name]
        for idx in m:
            cur = cur[int(idx)]
    return cur


def _run_request(req: dict):
    method = str(req.get("method", "GET")).upper()
    path = req.get("path", "/")
    url = API_BASE_URL.rstrip("/") + "/" + str(path).lstrip("/")
    headers = dict(req.get("headers") or {})
    params = req.get("params") or {}
    body = req.get("body")
    if body is not None and isinstance(body, (dict, list)) and "Content-Type" not in headers \
            and "content-type" not in {k.lower() for k in headers}:
        headers["Content-Type"] = "application/json"
    if body is None:
        return requests.request(method, url, headers=headers, params=params, timeout=API_TIMEOUT)
    if isinstance(body, (dict, list)):
        return requests.request(method, url, headers=headers, params=params,
                                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                timeout=API_TIMEOUT)
    return requests.request(method, url, headers=headers, params=params, data=body, timeout=API_TIMEOUT)


def _safe_response(resp):
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:5000]
    return {
        "status_code": resp.status_code,
        "headers": mask_sensitive(dict(resp.headers)),
        "body": mask_sensitive(body) if isinstance(body, (dict, list)) else str(body)[:5000],
    }


def _check(record, name: str, ok: bool, expected, actual, message: str):
    record["assert_detail"].append({
        "name": name,
        "expected": mask_sensitive(expected),
        "actual": mask_sensitive(actual),
        "passed": bool(ok),
    })
    assert ok, message


@pytest.mark.parametrize("case_id,case", CASES, ids=[c[0] for c in CASES])
def test_api_case(case_id, case):
    req = case.get("request") or {}
    expect = case.get("expect") or {}
    record = {
        "run_id": os.environ.get("DSH_RUN_ID", ""),
        "case_id": case_id,
        "status": "passed",
        "start_time": _now(),
        "assert_detail": [],
    }
    started = time.time()

    try:
        resp = _run_request(req)
        request_info = {
            "method": str(req.get("method", "GET")).upper(),
            "path": req.get("path", "/"),
            "headers": mask_sensitive(dict(req.get("headers") or {})),
            "params": mask_sensitive(req.get("params") or {}),
            "body": mask_sensitive(req.get("body")),
        }
        record["request"] = request_info
        record["req_content"] = json.dumps(request_info, ensure_ascii=False)
        record["response"] = _safe_response(resp)
        record["resp_content"] = json.dumps(record["response"], ensure_ascii=False)

        expected_status = expect.get("status_code", 200)
        _check(record, "status_code", resp.status_code == expected_status, expected_status, resp.status_code,
               f"[{case_id}] 期望状态码 {expected_status}，实际 {resp.status_code}，响应: {resp.text[:500]}")

        if expect.get("contains"):
            _check(record, "contains", str(expect["contains"]) in resp.text, expect["contains"],
                   resp.text[:500],
                   f"[{case_id}] 响应文本不包含 `{expect['contains']}`，实际: {resp.text[:500]}")

        if expect.get("json_path"):
            try:
                payload = resp.json()
            except ValueError:
                pytest.fail(f"[{case_id}] 响应不是合法 JSON: {resp.text[:300]}", pytrace=False)
            actual = _json_get(payload, expect["json_path"])
            _check(record, expect["json_path"], actual == expect.get("equals"), expect.get("equals"), actual,
                   f"[{case_id}] {expect['json_path']} 期望 {expect.get('equals')!r}，实际 {actual!r}")
    except BaseException as e:
        record["status"] = "failed"
        record["error_msg"] = str(e)[:2000]
        raise
    finally:
        record["end_time"] = _now()
        record["cost_ms"] = int((time.time() - started) * 1000)
        _write_runtime(record)
