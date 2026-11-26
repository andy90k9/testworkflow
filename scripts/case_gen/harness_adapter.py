#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek Harness 适配层。

1. 读取 config/providers.yaml 的 harness 段
2. 按 command / http 调用 Harness，并注入当前 run_id
3. 清理旧用例后分发产物到 cases/（保留 example.*.yaml）
4. 调用 validate_cases.py 做格式校验

也支持 provider: qa_casepipe —— 改由 qa-toolkit（qa-case-pipeline）生成三层用例，
产物同样落到 cases/functional|api/ui，并附带 Markdown + Excel 用例集。

用法:
  python3 scripts/case_gen/harness_adapter.py            # 调用 harness 生成并分发
  python3 scripts/case_gen/harness_adapter.py --dry-run  # 只检查配置与前置
"""
import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dsh_common import (  # noqa: E402
    load_config, load_providers, ensure_dirs, init_run_id,
    inject_run_id_into_cases, resolve, WORKFLOW_ROOT,
)
import qa_casepipe  # noqa: E402

PROVIDERS_PATH = WORKFLOW_ROOT / "config" / "providers.yaml"
CASE_TYPES = ("functional", "api", "ui")
CASE_DIR_KEYS = {"functional": "functional", "api": "api_cases", "ui": "ui_cases"}
PROVIDERS = ("harness", "qa_casepipe")


def _harness_output_dir(harness: dict) -> Path:
    out = harness.get("output_dir") or "records/harness-output"
    p = Path(out)
    return p if p.is_absolute() else (WORKFLOW_ROOT / p).resolve()


def _check_prerequisites(paths: dict) -> list:
    """前置检查：需求与代码目录非空。"""
    problems = []
    req_files = [p for p in paths["requirements"].iterdir() if p.is_file()] if paths["requirements"].exists() else []
    src_files = list(paths["source"].iterdir()) if paths["source"].exists() else []
    if not req_files:
        problems.append(f"需求目录为空: {paths['requirements']}（请先放入需求文档）")
    if not src_files:
        problems.append(f"代码目录为空: {paths['source']}（请先放入被测代码）")
    return problems


def _call_command(harness: dict, out_dir: Path, paths: dict, run_id: str) -> None:
    cmd_tpl = harness.get("command") or ""
    if not cmd_tpl.strip():
        raise SystemExit("providers.yaml -> harness.command 未配置（command 模式必填）")
    cmd = cmd_tpl.format(
        requirements=str(paths["requirements"]),
        source=str(paths["source"]),
        output=str(out_dir),
        run_id=run_id,
    )
    print(f"▶ 调用 harness（command）: {cmd}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          timeout=int(harness.get("timeout_sec", 600)))
    log = out_dir / "harness-command.log"
    log.write_text((proc.stdout or "") + "\n---STDERR---\n" + (proc.stderr or ""), encoding="utf-8")
    if proc.returncode != 0:
        print(f"harness 命令返回码 {proc.returncode}，日志: {log}")
        print((proc.stderr or proc.stdout or "")[-1500:])
        raise SystemExit(1)
    print(f"harness 执行完成，日志: {log}")


def _call_http(harness: dict, out_dir: Path, paths: dict, run_id: str) -> None:
    endpoint = harness.get("http_endpoint") or ""
    if not endpoint.strip():
        raise SystemExit("providers.yaml -> harness.http_endpoint 未配置（http 模式必填）")
    method = str(harness.get("http_method", "POST")).upper()
    payload = json.dumps({
        "run_id": run_id,
        "requirements_dir": str(paths["requirements"]),
        "source_dir": str(paths["source"]),
        "output_dir": str(out_dir),
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(harness.get("http_headers") or {})
    req = urllib.request.Request(endpoint, data=payload, headers=headers, method=method)
    print(f"▶ 调用 harness（http）: {method} {endpoint}")
    try:
        with urllib.request.urlopen(req, timeout=int(harness.get("timeout_sec", 600))) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            (out_dir / "harness-http-response.json").write_text(body, encoding="utf-8")
            print(f"harness 响应 {resp.status}，已存 response 文件")
    except urllib.error.URLError as e:
        raise SystemExit(f"harness HTTP 调用失败: {e}")


def _cleanup_old_cases(paths: dict) -> None:
    """清理上一次生成的用例，保留 example.*.yaml。"""
    for key in CASE_DIR_KEYS.values():
        d = paths[key]
        if not d.exists():
            continue
        for f in d.glob("*.y*ml"):
            if not f.name.startswith("example."):
                f.unlink()


def _distribute(out_dir: Path, paths: dict) -> dict:
    """把 harness 产物分发到 cases/ 三类目录，返回统计。"""
    stats = {}
    for ctype in CASE_TYPES:
        src = out_dir / ctype
        dst = paths[CASE_DIR_KEYS[ctype]]
        n = 0
        if src.exists():
            for f in sorted(src.glob("*.y*ml")):
                shutil.copy2(f, dst / f.name)
                n += 1
        stats[ctype] = n
    return stats


def _validate_cases() -> None:
    """调用 validate_cases.py 做格式校验，不通过直接中止。"""
    print("▶ 用例格式校验 ...")
    proc = subprocess.run([sys.executable, str(WORKFLOW_ROOT / "scripts" / "validate_cases.py")],
                          capture_output=True, text=True)
    print((proc.stdout or "").strip())
    if proc.returncode != 0:
        print("校验不通过，请修正用例后重新执行（或让生成引擎修正输出格式）")
        raise SystemExit(1)


def _generate_by_qa_casepipe(providers: dict, paths: dict, run_id: str) -> dict:
    """由 qa-toolkit（qa-case-pipeline）生成三层用例并落到 cases/。"""
    pipe = qa_casepipe.casepipe_config(providers)
    if not pipe["spec"]:
        raise SystemExit("providers.yaml -> qa_casepipe.spec 未配置（qa-case-pipeline spec 路径）")
    spec_path = resolve(pipe["spec"])
    if not spec_path.exists():
        raise SystemExit(f"qa-case-pipeline spec 不存在: {spec_path}")

    scripts_dir = qa_casepipe.find_scripts_dir(pipe["scripts_dir"])
    if not scripts_dir:
        raise SystemExit("未找到 qa-case-pipeline scripts 目录（gen_cases.py / fill_status.py）")
    print(f"▶ qa-case-pipeline scripts: {scripts_dir}")

    md_path = resolve(pipe["md"])
    xlsx_path = resolve(pipe["xlsx"])
    md_path.parent.mkdir(parents=True, exist_ok=True)
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)

    gen = qa_casepipe.generate(spec_path, md_path, xlsx_path, pipe["scripts_dir"])
    if not gen.get("ok"):
        print("gen_cases.py 执行失败：")
        print((gen.get("stderr") or gen.get("stdout") or "")[-1500:])
        raise SystemExit(1)
    print(f"用例集已生成: {md_path} | {xlsx_path}")

    stats = qa_casepipe.spec_to_cases(spec_path, paths["functional"], paths["api_cases"],
                                       paths["ui_cases"], run_id)
    for note in stats.get("notes") or []:
        print(f"[翻译提示] {note}")
    print(f"分发完成: 功能 {stats['functional']} | API {stats['api']} | UI {stats['ui']}")
    return stats


def main():
    parser = argparse.ArgumentParser(description="DeepSeek Harness 用例生成适配层")
    parser.add_argument("--dry-run", action="store_true", help="只检查配置与前置，不实际调用")
    args = parser.parse_args()

    cfg = load_config()
    paths = ensure_dirs(cfg)
    providers = load_providers()
    harness = providers.get("harness") or {}

    for problem in _check_prerequisites(paths):
        print(f"[前置缺失] {problem}")

    if not harness.get("enabled"):
        print("harness.enabled = false：适配层已就绪但未接入，跳过生成阶段。")
        print("如需启用，请在 config/providers.yaml 填写 harness 段并把 enabled 改为 true")
        return

    provider = str(harness.get("provider") or "harness").strip().lower()
    if provider not in PROVIDERS:
        print(f"harness.provider 取值非法: {provider}（应为 {' | '.join(PROVIDERS)}）")
        raise SystemExit(1)

    mode = harness.get("mode", "command")
    if provider == "harness" and mode not in ("command", "http"):
        print(f"harness.mode 取值非法: {mode}（应为 command | http）")
        raise SystemExit(1)

    run_id = init_run_id()
    out_dir = _harness_output_dir(harness)
    if args.dry_run:
        print(f"dry-run 通过: provider={provider} mode={mode} run_id={run_id} 产物落点={out_dir}")
        if provider == "qa_casepipe":
            pipe = qa_casepipe.casepipe_config(providers)
            spec_path = resolve(pipe["spec"]) if pipe["spec"] else None
            print(f"  spec: {spec_path}（存在={bool(spec_path and spec_path.exists())}）")
            print(f"  scripts: {qa_casepipe.find_scripts_dir(pipe['scripts_dir']) or '未找到'}")
        return

    if (cfg.get("execute") or {}).get("cleanup_cases_before_gen", True):
        _cleanup_old_cases(paths)

    if provider == "qa_casepipe":
        _generate_by_qa_casepipe(providers, paths, run_id)
        inject_run_id_into_cases(cfg, paths, run_id)
        _validate_cases()
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    if mode == "command":
        _call_command(harness, out_dir, paths, run_id)
    else:
        _call_http(harness, out_dir, paths, run_id)

    stats = _distribute(out_dir, paths)
    total = sum(stats.values())
    if total == 0:
        print(f"harness 未产出任何用例文件，请检查产物目录结构约定: {out_dir}")
        print("约定: functional/ api/ ui/ 三个子目录下放 .yaml 用例文件")
        raise SystemExit(1)
    inject_run_id_into_cases(cfg, paths, run_id)
    print(f"分发完成: 功能 {stats['functional']} | API {stats['api']} | UI {stats['ui']}（共 {total} 个文件）")

    _validate_cases()


if __name__ == "__main__":
    main()
