#!/bin/bash
# 重启 Docker app 容器（保留 MySQL 不动），以让 bind-mounted 代码生效

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "正在重启 app 容器..."
docker compose restart app
echo "等待容器就绪..."
sleep 3
docker compose ps app
