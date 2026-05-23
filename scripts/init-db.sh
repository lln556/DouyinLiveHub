#!/bin/bash
# 初始化数据库

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "正在初始化数据库..."
uv run python -c "from services.data_service import DataService; ds = DataService(); ds.create_tables()"

echo "数据库初始化完成！"
