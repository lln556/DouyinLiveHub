#!/bin/bash
# 运行测试套件

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

if [ ! -d ".venv" ]; then
    echo "虚拟环境不存在，请先运行 scripts/install.sh"
    exit 1
fi

uv run python -m unittest "$@"
