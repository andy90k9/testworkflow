#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 spec → DSH 用例转换器（v1.2）。

从规格文件读取 scenarios/api/ui，生成 DSH 三类 YAML，保留 source_id 追溯，
并在生成后做 ID 完整性硬校验。所有项目专属信息来自配置/输入，不在代码里写死。

产物：
  cases/<type>/*.yaml         生成的用例（原文件自动备份）
  reports/spec-conversion-report.json
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_common import (  # noqa: E402
    load_config, ensure_dirs, resolve, load_yaml_file,
    write_json, read_run_id, WORKFLOW_ROOT,
)


def _load_yaml(path: Path) -> dict:
    return load_yaml_file(path) or {} if path.exists() else {}


def _natural_key(value):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(value))]


def _sha1(value) -> str:
    return hashlib.sha1(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _find_spec(cfg: dict, paths: dict) -> Path:
    primary = resolve(cfg.get("spec_file", ""))
    if primary.exists():
        return primary
    pattern = str(cfg.get("spec_glob", ""))
    if pattern:
        candidates = sorted(WORKFLOW_GLOB(pattern))
    else:
        candidates = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and all(k in data for k in ("scenarios", "api", "ui")):
            return path
    raise SystemExit("未找到包含 scenarios/api/ui 的 spec 文件，请检查 spec-adapter 配置")


def WORKFLOW_GLOB(pattern: str) -> list:
    from dsh_common import WORKFLOW_ROOT
    return list(WORKFLOW_ROOT.glob(pattern))


def _build_id_maps(spec: dict, adapter: dict) -> dict:
    cfg = adapter.get("spec_adapter") or {}
    formats = cfg.get("id_formats") or {}
    maps = {}
    for layer, key in (("functional", "scenarios"), ("api", "api"), ("ui", "ui")):
        fmt = formats.get(layer) or f"{layer.upper()}-{{seq:03d}}"
        items = sorted(spec.get(key) or [], key=lambda x: _natural_key(x.get("id", "")))
        maps[layer] = {
            str(item.get("id")): fmt.format(seq=idx)
            for idx, item in enumerate(items, 1)
        }
    return maps


def _collect_existing_header_values(path: Path, adapter: dict = None) -> dict:
    acfg = ((adapter or {}).get("spec_adapter") or {})
    candidates = [path]
    backup_dir = resolve(str(acfg.get("backup_dir", "backups/cases")))
    if backup_dir.exists():
        candidates.extend(sorted(backup_dir.rglob(path.name)))
    values = {}
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            data = load_yaml_file(candidate) or []
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            headers = ((item.get("request") or {}).get("headers") or {})
            for key, value in headers.items():
                if str(key) not in values and value and "{{" not in str(value):
                    values[str(key)] = value
    return values


def _collect_existing_path_values(target: Path, templates: list, adapter: dict) -> dict:
    """从当前用例与历史备份中按路径模板反推 {{变量}} 的真实值。"""
    acfg = adapter.get("spec_adapter") or {}
    candidates = [target]
    backup_dir = resolve(str(acfg.get("backup_dir", "backups/cases")))
    if backup_dir.exists():
        candidates.extend(sorted(backup_dir.rglob(target.name)))
    paths = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = load_yaml_file(path) or []
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                value = (item.get("request") or {}).get("path")
                if value:
                    paths.append(str(value))

    values = {}
    # 不含变量的模板是静态路径（如 /model/square/page）。它们本身是合法用例路径，
    # 绝不能被当成某个 {{变量}} 的实例来反推值，否则 /model/square/{{modelId}}
    # 会把静态段 "page" 误判成 modelId。
    literal_templates = {
        re.sub(r"\{base\}", "", str(t)) for t in templates
        if not re.search(r"\{\{", str(t))
    }
    literal_paths = {c.split("?", 1)[0] for c in paths if c.split("?", 1)[0] in literal_templates}
    for template in templates:
        parts = re.split(r"(\{\{\s*[A-Za-z0-9_.-]+\s*\}\})", str(template))
        names = [re.sub(r"[{}]", "", p).strip() for p in parts if p.startswith("{{")]
        if not names:
            continue
        pattern = "^" + "".join(
            "(.+?)" if p.startswith("{{") else re.escape(p) for p in parts
        ) + "$"
        for candidate in paths:
            if candidate.split("?", 1)[0] in literal_paths:
                continue
            match = re.match(pattern, candidate)
            if not match:
                continue
            for name, value in zip(names, match.groups()):
                if value and name not in values:
                    values[name] = value
    return values


def _resolve_var(name: str, spec: dict, existing: dict, adapter: dict) -> str:
    env_value = os.environ.get(f"DSH_{name.upper()}")
    if env_value is not None:
        return env_value
    configured = (adapter.get("spec_adapter", {}).get("header_variables") or {})
    if name in configured:
        value = str(configured[name])
        env_ref = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
        if env_ref:
            env_value = os.environ.get(env_ref.group(1))
            if env_value:
                return env_value
        else:
            return value
    meta = spec.get("meta") or {}
    if name in meta:
        return str(meta[name])
    if name in existing:
        return existing[name]
    return "{{" + name + "}}"


def _resolve_text_vars(text: str, spec: dict, values: dict, adapter: dict) -> str:
    acfg = adapter.get("spec_adapter") or {}
    configured = {}
    for key in ("variables", "path_variables", "header_variables"):
        configured.update(acfg.get(key) or {})

    def repl(match):
        name = match.group(1)
        env_value = os.environ.get(f"DSH_{name.upper()}")
        if env_value:
            return env_value
        if name in configured:
            value = str(configured[name])
            env_ref = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
            if env_ref:
                nested = os.environ.get(env_ref.group(1))
                if nested:
                    return nested
            else:
                return value
        if name in values:
            return values[name]
        meta = spec.get("meta") or {}
        if name in meta:
            return str(meta[name])
        return match.group(0)

    return re.sub(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}", repl, str(text))


def _resolve_object_vars(obj, spec: dict, values: dict, adapter: dict):
    if isinstance(obj, dict):
        return {k: _resolve_object_vars(v, spec, values, adapter) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_object_vars(v, spec, values, adapter) for v in obj]
    if isinstance(obj, str):
        return _resolve_text_vars(obj, spec, values, adapter)
    return obj


def _parse_headers(raw, spec: dict, existing: dict, adapter: dict) -> dict:
    if isinstance(raw, dict):
        source = {str(k): str(v) for k, v in raw.items()}
    elif isinstance(raw, str):
        source = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            source[key.strip()] = value.strip()
    else:
        source = {}
    result = {}
    for key, value in source.items():
        result[key] = _resolve_text_vars(value, spec, existing, adapter)
    return result


def _parse_expect(item: dict, adapter: dict) -> dict:
    cfg = (adapter.get("spec_adapter", {}).get("assertion") or {})
    data = item.get("assert") or {}
    expect = {}
    if "status" in data:
        expect["status_code"] = data["status"]
    if "code" in data:
        expect["json_path"] = cfg.get("code_json_path", "$.code")
        expect["equals"] = data["code"]
    elif cfg.get("data_key_contains", True) and data.get("dataKeys"):
        expect["contains"] = str(data["dataKeys"][0])
    if not expect:
        expect["status_code"] = 200
    return expect


def _parse_path_and_params(item: dict, spec: dict, path_values: dict, adapter: dict) -> tuple:
    raw = str(item.get("path") or item.get("url") or "")
    meta = spec.get("meta") or {}
    base = str(meta.get("base") or "")
    if base and raw.startswith(base):
        raw = raw[len(base):]
    raw = raw.replace("{base}", "")

    # spec 的 path 字段常只含路径，query 单独放在 `query` 字段；两者都需合并。
    merged_query = ""
    query_field = item.get("query")
    if isinstance(query_field, str) and query_field.strip():
        merged_query = query_field.strip().lstrip("?")
    elif isinstance(query_field, dict):
        merged_query = urlencode({str(k): str(v) for k, v in query_field.items() if v is not None})

    url_field = str(item.get("url") or "")
    if not merged_query and "?" in url_field:
        merged_query = url_field.split("?", 1)[1]

    raw = _resolve_text_vars(raw, spec, path_values, adapter)
    parsed = urlparse(raw)
    path = parsed.path or "/"
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if merged_query:
        merged_query = _resolve_text_vars(merged_query, spec, path_values, adapter)
        for key, value in parse_qsl(merged_query, keep_blank_values=True):
            params.setdefault(key, value)
    return path, params


# spec 里用这些占位文本表示"该请求没有请求体"，不能当成真实 body 发出去。
_NO_BODY_SENTINELS = {"无请求体", "无", "none", "null", "-", "n/a", "无请求参数", "无body"}


_IDEMPOTENCY_KEYS = ("requestId", "request_id", "idempotencyKey", "idempotency_key", "reqId")


def _uniquify_idempotency(obj, run_id: str):
    """幂等键（requestId 等）必须每次运行唯一，否则二次执行会被服务端判为重放。

    服务端提示：BizException: 操作请求 ID 已被使用，请重新提交。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k) in _IDEMPOTENCY_KEYS and isinstance(v, str) and v:
                out[k] = v if (not run_id or v.endswith(str(run_id))) else f"{v}-{run_id}"
            else:
                out[k] = _uniquify_idempotency(v, run_id)
        return out
    if isinstance(obj, list):
        return [_uniquify_idempotency(v, run_id) for v in obj]
    return obj


def _functional_checks(item: dict, spec: dict, adapter: dict, run_id: str = "") -> dict:
    """把场景挂靠的 API 用例转成可机判 checks。

    功能场景的 steps/expected 是自然语言，执行器无法机判；沿 related 里的
    REG-API-* 生成真实 HTTP 断言，让功能链路能被断言而不是被跳过。
    """
    apis = {str(a.get("id")): a for a in (spec.get("api") or [])}
    checks = []
    for ref in item.get("related") or []:
        ref = str(ref)
        if not ref.startswith("REG-API-"):
            continue
        api = apis.get(ref)
        if not api:
            continue
        req = _parse_request(api, spec, {}, adapter) if "_parse_request" in globals() else None
        method = str(api.get("method", "GET")).upper()
        path = api.get("path") or ""
        if not path:
            continue
        av = api.get("assert") or {}
        expect = {"status_code": av.get("status", 200)}
        if "code" in av:
            expect["json_path"] = "$.code"
            expect["equals"] = av["code"]
        check = {"name": str(api.get("name") or ref), "request": {"method": method, "path": path}}
        # 有些接口的必填条件在 query 上（如 /knowledge-directory/tree?scope=maintainable），
        # 丢掉 query 会直接 500。
        query = api.get("query")
        if isinstance(query, dict) and query:
            check["request"]["query"] = query
        body = api.get("body_json")
        if body is not None:
            check["request"]["body"] = _uniquify_idempotency(body, run_id)
        headers = api.get("headers")
        if isinstance(headers, str) and headers.strip():
            parsed = {}
            for line in headers.splitlines():
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    k, v = line.split(":", 1)
                    parsed[k.strip()] = v.strip()
            headers = parsed
        if isinstance(headers, dict) and headers:
            kept = {}
            for k, v in headers.items():
                low = str(k).lower()
                # clientId 由执行器统一注入；Authorization 只在它是占位符时丢弃，
                # 因为负向用例必须保留显式的非法 token 才能断言 401。
                if low == "clientid":
                    continue
                if low == "authorization" and "{{" in str(v):
                    continue
                kept[k] = v
            if kept:
                check["request"]["headers"] = kept
        check["expect"] = expect
        keys = av.get("dataKeys") or []
        if keys:
            check["expect_data_keys"] = keys
        checks.append(check)
    return {"checks": checks} if checks else {}


def _parse_body(item: dict, spec: dict, values: dict, adapter: dict):
    if isinstance(item.get("body_json"), (dict, list)):
        body = item["body_json"]
    else:
        raw = item.get("body")
        if isinstance(raw, (dict, list)):
            body = raw
        elif isinstance(raw, str):
            text = raw.strip()
            if not text or text.lower() in _NO_BODY_SENTINELS:
                body = None
            else:
                try:
                    body = json.loads(text)
                except Exception:
                    body = {"raw": text}
        else:
            body = None
    if body is None:
        return None
    return _resolve_object_vars(body, spec, values, adapter)


def _parse_steps_text(text: str, expected: str) -> list:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        lines = ["按场景步骤执行"]
    steps = []
    for idx, line in enumerate(lines, 1):
        action = re.sub(r"^\d+\s*[.、)]\s*", "", line)
        steps.append({"step": idx, "action": action, "expected": expected or "符合场景预期"})
    return steps


def _selector(expr: str, adapter: dict = None) -> str:
    """把 Playwright locator DSL 降级为 EGO 可执行的 CSS 选择器。

    EGO 的 page 没有 locator()/getByRole()，只吃 CSS（支持 :has-text 与 ">> nth=N"）。
    因此 getByRole 必须按页面真实无障碍属性（aria-label）或文本精确降级，
    正则形式的 name 也要能解析，不能把 DSL 原样透传出去。
    """
    cfg = ((adapter or {}).get("spec_adapter") or {}).get("ui_selector") or {}
    outer = (adapter or {}).get("spec_adapter") or {}
    aliases = cfg.get("role_aliases") or {}
    name_attr = cfg.get("name_attr") or {}
    name_attr_default = str(cfg.get("name_attr_default") or "text")
    nth = str(cfg.get("nth_suffix") or "")
    expr = expr.strip()

    # 只有显式 .first() 才降级为 nth=0，避免误伤多元素场景
    if ".first()" in expr and not nth:
        nth = " >> nth=0"

    selected = expr
    overrides = outer.get("selector_overrides") or {}
    match = re.search(r"getByTestId\(\s*['\"]([^'\"]+)['\"]", expr)
    if match:
        tid = match.group(1)
        # spec 里的 testid 若在真实 DOM 中不存在，按配置换成真实容器
        selected = f'[data-testid="{overrides.get(tid, tid)}"]'
    else:
        # 同时支持 { name: '文本' } 与 { name: /文本/ }，避免正则字面量截断 DSL
        match = re.search(
            r"getByRole\(\s*['\"]([^'\"]+)['\"]\s*,\s*\{\s*name\s*:\s*['\"]([^'\"]+)['\"]", expr
        ) or re.search(r"getByRole\(\s*['\"]([^'\"]+)['\"]\s*,\s*\{\s*name\s*:\s*/([^/]+)/", expr)
        if match:
            role, name = match.group(1), match.group(2)
            base = aliases.get(role, role)
            attr = str(name_attr.get(role) or name_attr_default)
            # antd 会给两字中文按钮插入空格（"重试" → "重 试"），按配置换成渲染后的文本
            name = str((outer.get("text_overrides") or {}).get(name, name))
            if attr == "aria-label":
                selected = f'{base}[aria-label="{name}"]'
            elif "," in base:
                # 多标签（如 h1..h6）无法逐一分列，用 :is() 包一层保持单一表达式
                selected = f':is({base}):has-text("{name}")'
            else:
                selected = f'{base}:has-text("{name}")'
        else:
            # 无 name 的 getByRole 也要降级，不能把 DSL 原样透传
            match = re.search(r"getByRole\(\s*['\"]([^'\"]+)['\"]\s*\)", expr)
            if match:
                selected = aliases.get(match.group(1), match.group(1))
            else:
                # 用反向引用配对引号，避免 locator('aside[aria-label="x"]') 被内层双引号截断
                match = re.search(r"locator\(\s*(['\"])(.+?)\1\s*\)", expr)
                if match:
                    selected = match.group(2)
    if nth and not selected.endswith(nth):
        selected = selected + nth
    return selected


def _js_object_to_dict(text: str) -> dict:
    """把 JS 对象字面量宽松转成 dict（仅支持 mock 里出现的简单键值）。"""
    out = {}
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*:\s*(['\"])(.*?)\2", text):
        out[m.group(1)] = m.group(3)
    return out


def _deref(expr: str, variables: dict) -> str:
    """把 const 变量引用（如 region）还原成它对应的 locator 表达式。"""
    token = expr.strip().rstrip(";")
    return variables.get(token, expr)


def _route_for(item: dict, spec: dict, adapter: dict) -> str:
    """按 关联场景步骤 → 模块名映射 的顺序推导页面入口路由。"""
    related = str(item.get("related") or "")
    scenario = next(
        (s for s in ((spec or {}).get("scenarios") or []) if str(s.get("id")) == related), None
    )
    if scenario:
        match = re.search(r"(/#/[^\s\"\)]+)", str(scenario.get("steps") or ""))
        if match:
            return match.group(1)
    routes = (((adapter or {}).get("spec_adapter") or {}).get("ui_routes") or {})
    return str(routes.get(str(item.get("module") or "")) or "")


def _translate_ui_steps(item: dict, base_url: str, adapter: dict, manual: list, spec: dict = None) -> list:
    cfg = adapter.get("spec_adapter") or {}
    wait = str(cfg.get("ui_wait_seconds", 2))
    js = str(item.get("steps_playwright") or "")
    steps = []
    # const 变量 → 其 locator 表达式，供后续 region.click() 之类的引用回填
    variables = {}

    def route_for_current() -> str:
        return _route_for(item, spec or {}, adapter)

    for raw in js.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.search(r"page\.goto\(\s*[`'\"]([^`'\"]+)", line)
        if match:
            url = match.group(1).replace("${BASE}", base_url)
            if base_url and (not url or url.rstrip("/") == base_url.rstrip("/")):
                route = route_for_current()
                if route:
                    url = base_url + route
            elif base_url and url.startswith("/"):
                url = base_url + url
            steps.append({"action": f"goto {url}"})
            continue
        match = re.search(r"expect\(page\.url\(\)\)\.toContain\('([^']+)'\)", line)
        if match:
            steps.append({"action": f"expect_url_contains {match.group(1)}"})
            continue
        if "waitForURL" in line:
            steps.append({"action": f"wait {wait}"})
            manual.append(f"{item.get('id')}: waitForURL 正则需人工确认")
            continue
        if "page.unroute" in line:
            # CDP 注入无法真正撤销，改用 mock_off 在当前页面停用该 mock，等价于 unroute
            hint = re.search(r"portalUrl\(\s*['\"]([^'\"]+)['\"]", line)
            if hint:
                steps.append({"action": f"mock_off {hint.group(1)}"})
                manual.append(f"{item.get('id')}: unroute 已转为 mock_off（页面内停用 mock）")
            else:
                manual.append(f"{item.get('id')}: unroute 无法确定 URL，需人工补: {line[:80]}")
            continue
        # page.route(...) → mock_route / mock_stream，用 CDP 注入 fetch 覆盖实现
        match = re.search(r"page\.route\(\s*[^,]+,\s*route\s*=>\s*route\.fulfill\(\s*\{(.*)\}\s*\)\s*\)", line)
        if match:
            opts = match.group(1)
            url_hint = re.search(r"portalUrl\(\s*['\"]([^'\"]+)['\"]", line) \
                or re.search(r"['\"]([^'\"]*?/api/[^'\"]*)['\"]", line)
            needle = url_hint.group(1) if url_hint else ""
            status_m = re.search(r"status\s*:\s*(\d+)", opts)
            status = status_m.group(1) if status_m else "200"
            is_sse = "text/event-stream" in opts or "sse(" in opts
            if needle:
                if is_sse:
                    # 真实流式协议是 event:delta + data:{"text":...}，收尾 event:done。
                    # 用规范文本回复，避免依赖真实模型输出。
                    reply = "回归巡检回复"
                    sse = (
                        f"event:delta\ndata:{json.dumps({'text': reply}, ensure_ascii=False)}\n\n"
                        "event:done\ndata:{}\n\n"
                    )
                    steps.append({"action": f"mock_stream {needle} {sse}"})
                else:
                    body_m = re.search(r"body\s*:\s*(['\"])(.*?)\1", opts)
                    if body_m:
                        body = body_m.group(2)
                    elif "JSON.stringify" in opts:
                        # JSON.stringify({...}) → 取对象字面量，转成合法 JSON 文本
                        obj_m = re.search(r"JSON\.stringify\(\s*(\{.*\})\s*\)", opts)
                        raw_obj = obj_m.group(1) if obj_m else "{}"
                        body = json.dumps(_js_object_to_dict(raw_obj), ensure_ascii=False)
                    else:
                        body = '{"code":503,"msg":"mock 503"}'
                    steps.append({"action": f"mock_route {needle} {status} {body}"})
                continue
            manual.append(f"{item.get('id')}: page.route 无法确定 URL，需人工补: {line[:90]}")
            continue
        if any(flag in line for flag in ("page.route", "waitForEvent", "Promise.all")):
            manual.append(f"{item.get('id')}: 路由模拟需人工补 DSL: {line[:100]}")
            continue
        # 计数断言（卡片数 > 0）转成"网格可见"，等价于"列表非空且已渲染"
        match = re.search(r"\.locator\((['\"])(.+?)\1\)\.count\(\)", line)
        if match:
            steps.append({"action": f"expect_visible {match.group(2)}"})
            continue
        # const region = page.getByRole(...)：登记变量，不单独产步骤
        match = re.search(r"const\s+([A-Za-z_$][\w$]*)\s*=\s*(page\..+?);?\s*$", line)
        if match:
            variables[match.group(1)] = match.group(2).rstrip(";")
            continue
        match = re.search(r"await\s+(.+?)\.fill\((['\"])(.*?)\2\)", line)
        if match:
            steps.append({"action": f"fill {_selector(_deref(match.group(1), variables), adapter)} {match.group(3)}"})
            continue
        match = re.search(r"await\s+(.+?)\.click\(\)", line)
        if match:
            steps.append({"action": f"click {_selector(_deref(match.group(1), variables), adapter)}"})
            continue
        match = re.search(r"expect\((.+?)\)\.toContainText\((['\"])(.*?)\2\)", line)
        if match:
            steps.append({"action": f"expect_text {_selector(_deref(match.group(1), variables), adapter)} {match.group(3)}"})
            continue
        match = re.search(r"expect\((.+?)\)\.toBeVisible\(\)", line)
        if match:
            steps.append({"action": f"expect_visible {_selector(_deref(match.group(1), variables), adapter)}"})
            continue
        if line.startswith("const ") or line.startswith("await expect(page.locator"):
            manual.append(f"{item.get('id')}: 复杂 JS 断言需人工确认: {line[:100]}")

    if not any(s["action"].startswith("goto ") for s in steps):
        route = route_for_current()
        url = (base_url + route) if (base_url and route.startswith("/")) else (route or base_url or "/")
        steps.insert(0, {"action": f"goto {url}"})
        manual.append(f"{item.get('id')}: 原 JS 无 goto，已按关联场景/模块补齐入口 {url}")

    # 内容断言优先于 landmark 断言：region/aria-label 缺失是 a11y 缺陷，
    # 不应遮蔽"列表是否真的渲染"这一用户可见结果（landmark 缺失记入报告单独说明）。
    def _is_landmark(step) -> bool:
        action = str(step.get("action") or "")
        return action.startswith("expect_visible ") and '[role="region"]' in action

    landmarks = [s for s in steps if _is_landmark(s)]
    if landmarks and len(landmarks) < len(steps):
        steps[:] = [s for s in steps if not _is_landmark(s)] + landmarks

    # mock 是 CDP "新文档生效"注入，必须排在首个 goto 之前，否则本次导航用不上。
    mock_verbs = ("mock_route ", "mock_stream ")
    mocks = [s for s in steps if str(s.get("action") or "").startswith(mock_verbs)]
    if mocks:
        rest_steps = [s for s in steps if not str(s.get("action") or "").startswith(mock_verbs)]
        goto_idx = next(
            (i for i, s in enumerate(rest_steps) if str(s.get("action") or "").startswith("goto ")),
            0,
        )
        steps[:] = rest_steps[:goto_idx] + mocks + rest_steps[goto_idx:]

    # 试用面板里的控件要先打开面板才存在，否则 fill/click 必然 0 元素命中。
    trial_selectors = tuple(cfg.get("trial_panel_selectors") or ())
    if trial_selectors and any(
        trial_selectors[0] in str(s.get("action") or "") or
        any(t in str(s.get("action") or "") for t in trial_selectors)
        for s in steps
    ) and not any("model-card-trial" in str(s.get("action") or "") for s in steps):
        opener = str(cfg.get("trial_open_action") or 'click [data-testid="model-card-trial"] >> nth=0')
        insert_at = next(
            (i + 1 for i, s in enumerate(steps) if str(s.get("action") or "").startswith("goto ")),
            0,
        )
        steps.insert(insert_at, {"action": opener})
        manual.append(f"{item.get('id')}: 已自动补 open 试用面板步骤（面板内控件需先展开）")

    if cfg.get("ui_screenshot", True):
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", str(item.get("id"))).lower()
        steps.append({"action": f"screenshot {safe}"})
    if not steps:
        steps.append({"action": f"wait {wait}"})
    return steps


def _skip_fields(case_id: str, adapter: dict) -> dict:
    """按配置把"前提在本环境无法复现"的用例标记为跳过，并写明原因。"""
    reasons = ((adapter or {}).get("spec_adapter") or {}).get("ui_skip_cases") or {}
    reason = reasons.get(case_id) or reasons.get(str(case_id))
    if not reason:
        return {}
    return {"skip": True, "skip_reason": str(reason)}


def _tier(priority, adapter):
    mapping = (adapter.get("spec_adapter", {}).get("priority_tiers") or {})
    return mapping.get(str(priority), "extension")


def _source_fields(item, adapter, spec_version):
    return {
        "source_id": str(item.get("id")),
        "source_version": str(spec_version or "unspecified"),
        "action": "reuse_convert",
        "spec_hash": _sha1(item),
    }


def _backup(targets: list, adapter: dict) -> str:
    cfg = adapter.get("spec_adapter") or {}
    if not cfg.get("backup_existing", True):
        return ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = resolve(str(cfg.get("backup_dir", "cases/legacy"))) / stamp
    saved = []
    for path in targets:
        if path.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest = backup_dir / path.name
            shutil.copy2(path, dest)
            try:
                saved.append(os.path.relpath(dest, WORKFLOW_ROOT))
            except ValueError:
                saved.append(str(dest))
    return ", ".join(saved)


def _write_yaml(path: Path, items: list) -> None:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(items, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _collect_runtime_bindings(spec: dict) -> dict:
    """spec 中 dataKeys 的 dict 形式表示运行时从上一步响应提取变量。"""
    bindings = {}
    for item in (spec.get("api") or []):
        for candidate in (
            item.get("capture"),
            item.get("dataKeys"),
            (item.get("assert") or {}).get("dataKeys"),
        ):
            if isinstance(candidate, dict):
                bindings.update(candidate)
    return bindings


def _vars_in(value) -> list:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return sorted(set(re.findall(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}", text)))


def main() -> None:
    parser = argparse.ArgumentParser(description="通用 spec → DSH 用例转换与 ID 硬校验")
    parser.add_argument("--run-id", help="写入用例的 run_id；缺省读取 current-run.json 或 pending")
    parser.add_argument("--config", help="spec-adapter 配置路径；缺省读 workflow.yaml")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    generation = cfg.get("generation") or {}
    adapter_path = resolve(args.config or generation.get("spec_adapter_file", "config/spec-adapter.yaml"))
    adapter = _load_yaml(adapter_path)
    if not adapter:
        raise SystemExit(f"未找到适配器配置: {adapter_path}")
    acfg = adapter.get("spec_adapter") or {}

    spec_path = _find_spec(acfg, paths)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_bundle_hash = _sha1(spec)
    spec_version = (spec.get("meta") or {}).get("version", "")
    id_maps = _build_id_maps(spec, adapter)
    run_id = args.run_id or read_run_id() or acfg.get("run_id_placeholder", "pending")
    spec_rel = str(spec_path.relative_to(paths["workflow_root"]))

    functional, api_cases, ui_cases = [], [], []
    manual_notes = []

    for item in sorted(spec.get("scenarios") or [], key=lambda x: _natural_key(x.get("id", ""))):
        sid = str(item.get("id"))
        priority = str(item.get("priority", "P1"))
        functional.append({
            "case_id": id_maps["functional"][sid],
            "run_id": run_id,
            "title": item.get("name", sid),
            "module": item.get("chain", "-"),
            "priority": priority,
            "tier": _tier(priority, adapter),
            "precondition": item.get("precondition", ""),
            "related_req": sid,
            "source_file": spec_rel,
            **_source_fields(item, adapter, spec_version),
            "steps": _parse_steps_text(item.get("steps", ""), item.get("expected", "")),
            **_functional_checks(item, spec, adapter, run_id),
        })

    api_target = resolve(str((acfg.get("output") or {}).get("api", "cases/api/portal.api.yaml")))
    path_templates = [str(x.get("path") or x.get("url") or "") for x in (spec.get("api") or [])]
    existing_path_values = _collect_existing_path_values(api_target, path_templates, adapter)
    # spec 里带 capture 的变量由执行器运行时采集，不能沿用历史备份里推断出的旧值，
    # 否则会把上一次跑出来的脏值固化进用例（例如把静态段 "page" 当成 modelId）。
    captured_vars = set(_collect_runtime_bindings(spec))
    for name in captured_vars:
        existing_path_values.pop(name, None)

    for item in sorted(spec.get("api") or [], key=lambda x: _natural_key(x.get("id", ""))):
        sid = str(item.get("id"))
        priority = str(item.get("priority", "P1"))
        target = api_target
        existing = _collect_existing_header_values(target, adapter)
        path, params = _parse_path_and_params(item, spec, existing_path_values, adapter)
        request = {
            "method": str(item.get("method", "GET")).upper(),
            "path": path,
            "headers": _parse_headers(item.get("headers", ""), spec, existing, adapter),
        }
        if params:
            request["params"] = params
        body = _parse_body(item, spec, existing_path_values, adapter)
        # 空对象 {} 是 spec 明确要求的请求体（如 POST /rbac/runtime/context），
        # 只有 None（spec 写了"无请求体"）才真正不带 body。
        if body is not None:
            # 幂等键必须每次运行唯一，否则复跑会被服务端当作重放拒绝
            request["body"] = _uniquify_idempotency(body, run_id)
        expect = _parse_expect(item, adapter)
        note_bits = []
        if (item.get("assert") or {}).get("dataKeys"):
            note_bits.append("dataKeys=" + ",".join(map(str, item["assert"]["dataKeys"])))
        if item.get("negative"):
            note_bits.append("negative=" + str(item["negative"]))
        related = item.get("related")
        api_cases.append({
            "case_id": id_maps["api"][sid],
            "run_id": run_id,
            "title": item.get("name", sid),
            "module": item.get("module", "-"),
            "priority": priority,
            "tier": _tier(priority, adapter),
            "related_func": id_maps["functional"].get(str(related), str(related or "")),
            "related_req": sid,
            "source_file": spec_rel,
            **_source_fields(item, adapter, spec_version),
            "request": request,
            "expect": expect,
            # capture 让执行器能从本用例响应里提取变量，供后续用例复用。
            **({"capture": item["capture"]} if isinstance(item.get("capture"), dict) else {}),
            **({"note": "；".join(note_bits)} if note_bits else {}),
        })

    ui_base = str(acfg.get("ui_base_url") or "")
    for item in sorted(spec.get("ui") or [], key=lambda x: _natural_key(x.get("id", ""))):
        sid = str(item.get("id"))
        priority = str(item.get("priority", "P1"))
        related = item.get("related")
        raw_js = str(item.get("steps_playwright") or "")
        manual_reasons = [str(p) for p in (acfg.get("ui_manual_patterns") or []) if str(p) in raw_js]
        ui_cases.append({
            "case_id": id_maps["ui"][sid],
            "run_id": run_id,
            "title": item.get("name", sid),
            "module": item.get("module", "-"),
            "priority": priority,
            "tier": _tier(priority, adapter),
            "precondition": item.get("precondition_ui", ""),
            "related_func": id_maps["functional"].get(str(related), str(related or "")),
            "related_req": sid,
            "source_file": spec_rel,
            **_source_fields(item, adapter, spec_version),
            "expected": item.get("expected", ""),
            "steps": _translate_ui_steps(item, ui_base, adapter, manual_notes, spec),
            **({"needs_manual_context": True, "manual_context_reasons": manual_reasons} if manual_reasons else {}),
            **_skip_fields(id_maps["ui"][sid], adapter),
        })

    runtime_bindings = _collect_runtime_bindings(spec)
    for case in api_cases:
        pending = _vars_in(case)
        if pending:
            case["needs_runtime_binding"] = True
            case["runtime_binding_spec"] = {name: runtime_bindings.get(name, "") for name in pending}
    unresolved_variables = _vars_in(api_cases + ui_cases + functional)

    output = acfg.get("output") or {}
    targets = {
        "functional": resolve(str(output.get("functional", "cases/functional/portal.functional.yaml"))),
        "api": resolve(str(output.get("api", "cases/api/portal.api.yaml"))),
        "ui": resolve(str(output.get("ui", "cases/ui/portal.ui.yaml"))),
    }
    backup = _backup(list(targets.values()), adapter)
    for bundle in (functional, api_cases, ui_cases):
        for case in bundle:
            case["spec_bundle_hash"] = spec_bundle_hash
    _write_yaml(targets["functional"], functional)
    _write_yaml(targets["api"], api_cases)
    _write_yaml(targets["ui"], ui_cases)

    expected_ids = {
        str(x.get("id")) for x in (spec.get("scenarios") or []) + (spec.get("api") or []) + (spec.get("ui") or [])
    }
    written = [x["source_id"] for x in functional + api_cases + ui_cases]
    missing = sorted(expected_ids - set(written))
    extra = sorted(set(written) - expected_ids)
    duplicates = sorted({x for x in written if written.count(x) > 1})
    check = (adapter.get("spec_adapter", {}).get("id_check") or {})
    ok = True
    if check.get("require_all_spec_ids", True) and missing:
        ok = False
    if check.get("fail_on_duplicate", True) and (duplicates or extra):
        ok = False

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "spec_file": spec_rel,
        "spec_hash": spec_bundle_hash,
        "spec_version": spec_version,
        "run_id": run_id,
        "counts": {"functional": len(functional), "api": len(api_cases), "ui": len(ui_cases)},
        "tier_distribution": {
            layer: {tier: sum(1 for x in items if x.get("tier") == tier)
                    for tier in ("core", "extension", "temporary")}
            for layer, items in (("functional", functional), ("api", api_cases), ("ui", ui_cases))
        },
        "id_check": {"ok": ok, "missing": missing, "extra": extra, "duplicates": duplicates},
        "id_maps": id_maps,
        "manual_notes": manual_notes,
        "runtime_bindings": runtime_bindings,
        "unresolved_variables": unresolved_variables,
        "backup": backup,
        "outputs": {k: str(v.relative_to(paths["workflow_root"])) for k, v in targets.items()},
    }
    report_path = resolve(str(acfg.get("report", "reports/spec-conversion-report.json")))
    write_json(report_path, report)

    print(f"转换完成: FUNC {len(functional)} / API {len(api_cases)} / UI {len(ui_cases)}")
    print(f"转换报告: {report_path}")
    print(f"ID 校验: {'通过' if ok else '失败'}（缺失 {len(missing)} / 重复 {len(duplicates)} / 多余 {len(extra)}）")
    if manual_notes:
        print(f"待人工确认的复杂 DSL: {len(manual_notes)} 条（见转换报告）")
    if unresolved_variables:
        print(f"待运行时绑定变量: {', '.join(unresolved_variables)}（见转换报告 runtime_bindings）")
    if not ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
