#!/bin/bash
# 录制 docs/screenshots/*.png（全部虚构 mock 数据）。
#
# 这个 sh 是个薄壳：所有逻辑（拉起 ephemeral app、灌 mock 数据、Playwright 截图、
# 关进程）都在 tests/screenshots/capture.py 里。脚本本身不解析 .env，靠 capture.py
# 顶层的 load_dotenv 拿 AUTH_*/SECRET_KEY。
#
# 前置：
#   - .venv 已安装（scripts/install.sh）
#   - Playwright chromium 浏览器已下载：uv run playwright install chromium
#   - docker compose 已启好测试库 douyin_live_test（端口 3307）
#   - .env 已配置 AUTH_USERNAME / AUTH_PASSWORD / SECRET_KEY

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

if [ ! -d ".venv" ]; then
    echo "虚拟环境不存在，请先运行 scripts/install.sh" >&2
    exit 1
fi

if [ ! -f ".env" ]; then
    echo ".env 不存在，请先复制 .env.example 并配置" >&2
    exit 1
fi

exec uv run python tests/screenshots/capture.py
