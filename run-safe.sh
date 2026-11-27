#!/usr/bin/env bash
# 以「用例文件不含凭证」的方式跑 DSH 流水线。
#
# 背景：API 用例里的 Authorization 需要真实 SSO accessToken，但 token 不应提交进仓库。
# 做法：运行前把 __DSH_API_TOKEN__ 占位符渲染成真实 token，运行结束后（含异常/中断）
#      用 trap 回填占位符，保证 cases/api/portal.api.yaml 里始终是占位符。
#
# 用法：
#   bash run-safe.sh                    # 全链路，跳过 DB 回流
#   bash run-safe.sh --no-db            # 同上（显式）
#   DSH_TOKEN_FILE=/path/to/token bash run-safe.sh
set -euo pipefail

WORKFLOW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WORKFLOW_DIR"

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

TOKEN_FILE="${DSH_TOKEN_FILE:-}"
CASE_FILE="${DSH_API_CASE_FILE:-}"
PLACEHOLDER="__DSH_API_TOKEN__"

if [[ -z "$TOKEN_FILE" ]]; then
  echo "请通过 DSH_TOKEN_FILE 指定 token 文件路径（单行 accessToken）。" >&2
  exit 1
fi
if [[ -z "$CASE_FILE" ]]; then
  CASE_FILE="$("$PY" - <<'PYCFG'
import pathlib, yaml
cfg = yaml.safe_load(pathlib.Path("config/spec-adapter.yaml").read_text(encoding="utf-8")) or {}
print(((cfg.get("spec_adapter") or {}).get("output") or {}).get("api", ""))
PYCFG
)"
fi
if [[ -z "$CASE_FILE" ]]; then
  echo "请在 config/spec-adapter.yaml 的 spec_adapter.output.api 配置 API 用例文件。" >&2
  exit 1
fi
if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "缺少 token 文件: $TOKEN_FILE" >&2
  exit 1
fi

TOKEN="$(tr -d '\n\r' < "$TOKEN_FILE")"
if [[ -z "$TOKEN" ]]; then
  echo "token 文件为空: $TOKEN_FILE" >&2
  exit 1
fi

# 先把规格转换成用例（占位符形式），再做 token 渲染，避免转换器覆盖真实 token。
"$PY" scripts/spec_to_cases.py

restore() {
  local rc=$?
  "$PY" - "$CASE_FILE" "$TOKEN" "$PLACEHOLDER" <<'PY' || true
import sys, pathlib
path, token, placeholder = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
if p.exists():
    p.write_text(p.read_text(encoding="utf-8").replace(token, placeholder), encoding="utf-8")
PY
  trap - EXIT INT TERM
  exit $rc
}
trap restore EXIT INT TERM

"$PY" - "$CASE_FILE" "$TOKEN" "$PLACEHOLDER" <<'PY'
import sys, pathlib
path, token, placeholder = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
text = p.read_text(encoding="utf-8")
if token in text:
    print("用例文件已含真实 token，先回填占位符再渲染")
    text = text.replace(token, placeholder)
p.write_text(text.replace(placeholder, token), encoding="utf-8")
print(f"已渲染 {text.count(placeholder)} 处 token")
PY

# 不能用 exec：exec 会替换当前 shell，trap 随之失效，token 就留在用例文件里了。
set +e
"$PY" scripts/run_pipeline.py --skip-gen "$@"
exit $?