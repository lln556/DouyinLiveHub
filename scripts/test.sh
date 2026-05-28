#!/bin/bash
# 测试运行器
#
# 用法:
#   ./scripts/test.sh                # 全跑（含 e2e）
#   ./scripts/test.sh unit           # 只跑非 e2e/非 integration 的（含旧 unittest）
#   ./scripts/test.sh integration    # L1 API 集成
#   ./scripts/test.sh e2e            # L2 浏览器 E2E
#   ./scripts/test.sh legacy         # 旧 unittest 兼容入口（python -m unittest）
#   ./scripts/test.sh all            # = 默认
#
# 任意子命令后追加的参数会透传给 pytest，例如：
#   ./scripts/test.sh e2e --headed --slowmo 500

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

if [ ! -d ".venv" ]; then
    echo "虚拟环境不存在，请先运行 scripts/install.sh"
    exit 1
fi

CMD="${1:-all}"
shift || true

case "$CMD" in
    unit)
        uv run pytest tests/ -m "not e2e and not integration" "$@"
        ;;
    integration)
        uv run pytest tests/integration/ "$@"
        ;;
    e2e)
        uv run pytest tests/e2e/ "$@"
        ;;
    legacy)
        uv run python -m unittest "$@"
        ;;
    all|*)
        uv run pytest tests/ "$@"
        ;;
esac
