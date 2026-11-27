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
from urllib.parse import parse_qsl, urlparse

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
    for template in templates:
        parts = re.split(r"(\{\{\s*[A-Za-z0-9_.-]+\s*\}\})", str(template))
        names = [re.sub(r"[{}]", "", p).strip() for p in parts if p.startswith("{{")]
        if not names:
            continue
        pattern = "^" + "".join(
            "(.+?)" if p.startswith("{{") else re.escape(p) for p in parts
        ) + "$"
        for candidate in paths:
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

    raw = _resolve_text_vars(raw, spec, path_values, adapter)
    parsed = urlparse(raw)
    path = parsed.path or "/"
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return path, params


def _parse_body(item: dict, spec: dict, values: dict, adapter: dict):
    if isinstance(item.get("body_json"), (dict, list)):
        body = item["body_json"]
    else:
        raw = item.get("body")
        if isinstance(raw, (dict, list)):
            body = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                body = json.loads(raw)
            except Exception:
                body = {"raw": raw}
        else:
            body = {}
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


def _selector(expr: str) -> str:
    expr = expr.strip()
    match = re.search(r"getByTestId\(\s*['\"]([^'\"]+)['\"]", expr)
    if match:
        return f'[data-testid="{match.group(1)}"]'
    match = re.search(
        r"getByRole\(\s*['\"]([^'\"]+)['\"]\s*,\s*\{\s*name\s*:\s*['\"]([^'\"]+)['\"]", expr
    )
    if match:
        return f"{match.group(1)}:has-text(\"{match.group(2)}\")"
    match = re.search(r"locator\(\s*['\"]([^'\"]+)['\"]", expr)
    if match:
        return match.group(1)
    return expr


def _translate_ui_steps(item: dict, base_url: str, adapter: dict, manual: list, spec: dict = None) -> list:
    cfg = adapter.get("spec_adapter") or {}
    wait = str(cfg.get("ui_wait_seconds", 2))
    js = str(item.get("steps_playwright") or "")
    steps = []

    for raw in js.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.search(r"page\.goto\(\s*[`'\"]([^`'\"]+)", line)
        if match:
            url = match.group(1).replace("${BASE}", base_url)
            if base_url and url.startswith("/"):
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
        if any(flag in line for flag in ("page.route", "page.unroute", "waitForEvent", "Promise.all", ".count()")):
            manual.append(f"{item.get('id')}: 路由模拟/计数断言需人工补 DSL: {line[:100]}")
            continue
        match = re.search(r"await\s+(.+?)\.fill\('([^']*)'\)", line)
        if match:
            steps.append({"action": f"fill {_selector(match.group(1))} {match.group(2)}"})
            continue
        match = re.search(r"await\s+(.+?)\.click\(\)", line)
        if match:
            steps.append({"action": f"click {_selector(match.group(1))}"})
            continue
        match = re.search(r"expect\((.+?)\)\.toContainText\('([^']*)'\)", line)
        if match:
            steps.append({"action": f"expect_text {_selector(match.group(1))} {match.group(2)}"})
            continue
        match = re.search(r"expect\((.+?)\)\.toBeVisible\(\)", line)
        if match:
            steps.append({"action": f"expect_visible {_selector(match.group(1))}"})
            continue
        if line.startswith("const ") or line.startswith("await expect(page.locator"):
            manual.append(f"{item.get('id')}: 复杂 JS 断言需人工确认: {line[:100]}")

    if not any(s["action"].startswith("goto ") for s in steps):
        related = str(item.get("related") or "")
        scenario = next(
            (s for s in ((spec or {}).get("scenarios") or []) if str(s.get("id")) == related), None
        )
        route = ""
        if scenario:
            match = re.search(r"(/#/[^\s\"\)]+)", str(scenario.get("steps") or ""))
            if match:
                route = match.group(1)
        url = (base_url + route) if (base_url and route.startswith("/")) else (route or base_url or "/")
        steps.insert(0, {"action": f"goto {url}"})
        manual.append(f"{item.get('id')}: 原 JS 无 goto，已按关联场景补齐入口 {url}")

    if cfg.get("ui_screenshot", True):
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", str(item.get("id"))).lower()
        steps.append({"action": f"screenshot {safe}"})
    if not steps:
        steps.append({"action": f"wait {wait}"})
    return steps


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
        })

    api_target = resolve(str((acfg.get("output") or {}).get("api", "cases/api/portal.api.yaml")))
    path_templates = [str(x.get("path") or x.get("url") or "") for x in (spec.get("api") or [])]
    existing_path_values = _collect_existing_path_values(api_target, path_templates, adapter)

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
        if body:
            request["body"] = body
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
            **({"note": "；".join(note_bits)} if note_bits else {}),
        })

    ui_base = str(acfg.get("ui_base_url") or "")
    for item in sorted(spec.get("ui") or [], key=lambda x: _natural_key(x.get("id", ""))):
        sid = str(item.get("id"))
        priority = str(item.get("priority", "P1"))
        related = item.get("related")
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
