#!/bin/bash
# 下载用户等级图标

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "正在下载用户等级图标..."
uv run python utils/download_level.py

echo "下载完成！"
