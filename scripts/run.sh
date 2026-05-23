#!/bin/bash
# 启动 Web 版应用

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# 确保虚拟环境存在
if [ ! -d ".venv" ]; then
    echo "虚拟环境不存在，正在安装依赖..."
    uv sync
fi

echo "正在启动应用..."
uv run python app.py
