#!/bin/bash
# Cookie 测活手动验证：打印探测接口原始响应与判定结果
#
# 用法:
#   ./scripts/check_cookie.sh                          # 用 .env 中的 DOUYIN_COOKIE
#   DOUYIN_COOKIE="ttwid=xxx" ./scripts/check_cookie.sh  # 用环境变量覆盖（dotenv 不覆盖已有环境变量）
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

uv run python -m services.cookie_health
