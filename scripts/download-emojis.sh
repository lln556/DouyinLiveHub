#!/bin/bash
# 下载抖音官方表情包并生成前端映射表

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "正在同步抖音官方表情包..."
if command -v uv >/dev/null 2>&1; then
    uv run python utils/download_emoji.py "$@"
else
    .venv/bin/python utils/download_emoji.py "$@"
fi

echo "同步完成！"
