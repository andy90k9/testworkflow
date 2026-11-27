#!/usr/bin/env bash
# 安全执行入口：仓库内用例只保留 token 占位符，真实 token 仅在运行期临时副本中使用。
#
# 用法：
#   DSH_TOKEN_FILE=/path/to/token bash run-safe.sh
#   DSH_TOKEN_FILE=/path/to/token bash run-safe.sh --dry-db
#   DSH_TOKEN_FILE=/path/to/token bash run-safe.sh --only api
set -euo pipefail

WORKFLOW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WORKFLOW_DIR"

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

TOKEN_FILE="${DSH_TOKEN_FILE:-}"
if [[ -z "$TOKEN_FILE" ]]; then
  echo "请通过 DSH_TOKEN_FILE 指定 token 文件路径（单行 accessToken）。" >&2
  exit 1
fi
if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "缺少 token 文件: $TOKEN_FILE" >&2
  exit 1
fi

set +e
"$PY" scripts/run_pipeline.py --token-file "$TOKEN_FILE" "$@"
exit $?
