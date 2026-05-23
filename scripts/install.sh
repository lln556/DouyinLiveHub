#!/bin/bash
# 安装项目依赖

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "正在使用 uv 安装依赖..."
uv sync

echo "依赖安装完成！"
