#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 用例 Schema 定义与校验。

三类用例统一用 YAML 管理，所有用例必须带 run_id：
- functional：人工评审为主，是 API/UI 用例的来源
- api：结构化 request/expect，可被 tests/api 数据驱动执行
- ui：结构化步骤 DSL，可被 tests/ui 翻译为 Playwright 操作或 EGO Skill 调用
"""
import json

# ---------- 公共字段 ----------
COMMON_REQUIRED = ["run_id", "case_id", "title", "module", "priority", "steps"]
COMMON_FIELDS_HINT = {
    "run_id": "所属批次ID，由流水线统一注入",
    "case_id": "用例编号，全局唯一，建议 FUNC-001 / API-001 / UI-001",
    "title": "用例标题",
    "module": "所属功能模块",
    "priority": "优先级 P0/P1/P2/P3",
    "precondition": "前置条件（可选）",
    "related_req": "关联需求编号（可选，便于追溯）",
    "steps": "操作步骤列表",
}

# ---------- 功能用例 ----------
FUNCTIONAL_SCHEMA = {
    "required": COMMON_REQUIRED,
    "steps_item_required": ["step", "action", "expected"],
}

# ---------- API 用例 ----------
API_SCHEMA = {
    "required": ["run_id", "case_id", "title", "module", "priority", "request", "expect"],
    "request_required": ["method", "path"],
    "request_fields_hint": {
        "method": "HTTP 方法 GET/POST/PUT/DELETE/PATCH",
        "path": "接口路径，如 /api/login（相对 api_base_url）",
        "headers": "请求头 dict（可选）",
        "params": "URL query 参数 dict（可选）",
        "body": "请求体 dict/list（可选）",
    },
    "expect_fields_hint": {
        "status_code": "期望 HTTP 状态码（可选，默认 200）",
        "json_path": "期望的响应 JSON 路径，如 $.code（可选）",
        "equals": "与 json_path 对应的期望值",
        "contains": "响应文本应包含的子串（可选）",
    },
}

# ---------- UI 用例 ----------
UI_SCHEMA = {
    "required": COMMON_REQUIRED,
    "steps_item_required": ["action"],
    "supported_actions": [
        "goto <url>", "click <selector>", "fill <selector> <text>",
        "press <selector> <key>", "check <selector>", "select <selector> <value>",
        "hover <selector>", "wait <seconds>", "screenshot <name>",
        "expect_url_contains <fragment>", "expect_text <selector> <text>",
        "expect_visible <selector>",
        "mock_route <url_contains> <status> <json_body> [times]",
        "mock_stream <url_contains> <sse_text>",
        "mock_off <url_contains>",
        "download <trigger_selector> <save_path>", "upload <selector> <path|path>",
        "popup <trigger_selector>", "accept_dialog <text?>", "dismiss_dialog <text?>",
        "mouse_click <x,y>", "mouse_move <x,y>", "mouse_wheel <x,y>",
        "key_press <key>", "type_text <text>", "paste <text>", "insert_text <text>",
        "reload", "wait_function <expression>", "skill <name> <json_args>",
    ],
}

SUPPORTED_ACTIONS = {
    "goto", "click", "fill", "press", "check", "select", "hover", "wait",
    "screenshot", "expect_url_contains", "expect_text", "expect_visible",
    "mock_route", "mock_stream", "mock_off",
    "download", "upload", "popup", "accept_dialog", "dismiss_dialog",
    "mouse_click", "mouse_move", "mouse_wheel", "key_press", "type_text",
    "paste", "insert_text", "reload", "wait_function",
}
SKILL_WHITELIST = set()

PRIORITY_SET = {"P0", "P1", "P2", "P3"}


def _validate_ui_action(action: str, source: str, idx: int, errors: list, warnings: list) -> None:
    parts = action.strip().split(" ", 2)
    verb = parts[0].lower() if parts else ""
    if verb != "skill":
        if verb and verb not in SUPPORTED_ACTIONS:
            warnings.append(f"{source}: steps[{idx}] 动作 `{verb}` 不在标准 DSL 中，执行时可能失败")
        return
    if len(parts) < 2 or not parts[1]:
        errors.append(f"{source}: steps[{idx}] skill 动作缺少技能名")
        return
    if SKILL_WHITELIST and parts[1] not in SKILL_WHITELIST:
        errors.append(f"{source}: steps[{idx}] 技能 `{parts[1]}` 不在 ego_browser.skill_whitelist 中")
    elif not SKILL_WHITELIST:
        warnings.append(f"{source}: steps[{idx}] 未配置技能白名单，执行时将以 provider 配置为准")
    if len(parts) > 2:
        try:
            json.loads(parts[2])
        except json.JSONDecodeError as e:
            errors.append(f"{source}: steps[{idx}] skill 参数不是合法 JSON: {e}")


def validate_case(data: dict, case_type: str, source: str):
    """校验单条用例，返回 (错误列表, 警告列表)。"""
    errors, warnings = [], []
    schema = {"functional": FUNCTIONAL_SCHEMA, "api": API_SCHEMA, "ui": UI_SCHEMA}.get(case_type)
    if schema is None:
        return [f"{source}: 未知的用例类型 {case_type}"], warnings
    if not isinstance(data, dict):
        return [f"{source}: 用例根节点必须是 dict"], warnings

    for key in schema["required"]:
        if key not in data or data[key] in (None, "", []):
            errors.append(f"{source}: 缺少必填字段 `{key}`")

    if "priority" in data and data["priority"] not in PRIORITY_SET:
        errors.append(f"{source}: priority 必须是 {'/'.join(sorted(PRIORITY_SET))}")

    steps = data.get("steps")
    if isinstance(steps, list):
        req_step = schema.get("steps_item_required", ["action"])
        for idx, st in enumerate(steps, 1):
            if not isinstance(st, dict):
                errors.append(f"{source}: steps[{idx}] 必须是 dict")
                continue
            for key in req_step:
                if key not in st or st[key] in (None, ""):
                    errors.append(f"{source}: steps[{idx}] 缺少字段 `{key}`")

    if case_type == "api" and isinstance(data.get("request"), dict):
        for key in API_SCHEMA["request_required"]:
            if key not in data["request"]:
                errors.append(f"{source}: request 缺少必填字段 `{key}`")
        expect = data.get("expect") or {}
        if "json_path" in expect and "equals" not in expect:
            errors.append(f"{source}: expect 指定了 json_path 但缺少 equals")

    if case_type == "ui" and isinstance(steps, list):
        for idx, st in enumerate(steps, 1):
            if isinstance(st, dict):
                _validate_ui_action(str(st.get("action", "")), source, idx, errors, warnings)

    if "related_req" not in data:
        warnings.append(f"{source}: 建议补充 related_req 便于需求追溯")

    return errors, warnings


def validate_directory(directory, case_type: str):
    """校验目录下所有 YAML 用例。返回 (全部错误, 全部警告, 用例数)。"""
    from dsh_common import selected_case_files, load_yaml_file

    errors, warnings, count = [], [], 0
    for path in selected_case_files(directory):
        count += 1
        try:
            data = load_yaml_file(path)
        except Exception as e:
            errors.append(f"{path.name}: YAML 解析失败 - {e}")
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            e, w = validate_case(item, case_type, path.name)
            errors.extend(e)
            warnings.extend(w)
    return errors, warnings, count


def main():
    import argparse
    from dsh_common import load_config, ensure_dirs, load_providers

    global SKILL_WHITELIST
    parser = argparse.ArgumentParser(description="DSH 用例格式校验")
    parser.add_argument("--type", choices=["functional", "api", "ui", "all"], default="all")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    ego = load_providers().get("ego_browser") or {}
    SKILL_WHITELIST = set(ego.get("skill_whitelist") or [])

    path_map = {
        "functional": paths["functional"],
        "api": paths["api_cases"],
        "ui": paths["ui_cases"],
    }
    targets = path_map if args.type == "all" else {args.type: path_map[args.type]}

    total_errors, total_warnings, total_cases = [], [], 0
    for ctype, d in targets.items():
        e, w, c = validate_directory(d, ctype)
        total_errors.extend(e)
        total_warnings.extend(w)
        total_cases += c

    print(f"校验用例文件数: {total_cases}")
    for w in total_warnings:
        print(f"[警告] {w}")
    for e in total_errors:
        print(f"[错误] {e}")
    if total_errors:
        print(f"结果: 校验不通过（{len(total_errors)} 个错误）")
        raise SystemExit(1)
    print("结果: 校验通过")


if __name__ == "__main__":
    main()
