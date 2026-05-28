"""pytest 全局 fixture。

⚠️ 关键约定
─────────────
import 任何业务模块（services.*、models.*、app）之前，必须先 load tests/.env.test，
否则 config.py 的 load_dotenv() 会读到项目根 .env 里的 DATABASE_URL（dev 库），
后续测试就会把 dev 数据库的业务表 truncate 掉。

dotenv 默认 override=False：先 load 测试 env 把 DATABASE_URL 锁死，
再 load dev .env 拿 AUTH_USERNAME / AUTH_PASSWORD / SECRET_KEY 等共享变量。
"""
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "tests" / ".env.test")
load_dotenv(ROOT / ".env")

import os  # noqa: E402

import pytest  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from sqlalchemy import text  # noqa: E402

from services.data_service import DataService  # noqa: E402


# ───────── session 级 ─────────

@pytest.fixture(scope="session")
def base_url() -> str:
    port = os.getenv("APP_PORT", "7654")
    return f"http://localhost:{port}"


@pytest.fixture(scope="session")
def auth() -> dict:
    return {
        "username": os.environ["AUTH_USERNAME"],
        "password": os.environ["AUTH_PASSWORD"],
    }


@pytest.fixture(scope="session")
def storage_state(base_url, auth, tmp_path_factory):
    """登录一次复用 cookie，避免每个测试都跑一遍登录流程。"""
    state_file = tmp_path_factory.mktemp("auth") / "state.json"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"{base_url}/login")
        page.fill("input[name=username]", auth["username"])
        page.fill("input[name=password]", auth["password"])
        page.click("button[type=submit]")
        page.wait_for_url(f"{base_url}/")
        ctx.storage_state(path=str(state_file))
        browser.close()
    return str(state_file)


@pytest.fixture(scope="session")
def data_service() -> DataService:
    """指向 douyin_live_test 的 DataService；create_tables 幂等。"""
    ds = DataService()
    ds.create_tables()
    return ds


# ───────── function 级 ─────────

_TRUNCATE_TABLES = [
    "chat_messages",
    "gift_messages",
    "user_contributions",
    "room_stats",
    "system_events",
    "live_sessions",
    "live_rooms",
]


@pytest.fixture(autouse=True)
def clean_db(data_service):
    """每个测试前 truncate 业务表。autouse 保证零遗漏。"""
    sess = data_service.get_session()
    try:
        sess.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for tbl in _TRUNCATE_TABLES:
            sess.execute(text(f"TRUNCATE TABLE {tbl}"))
        sess.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        sess.commit()
    finally:
        sess.close()
    yield


@pytest.fixture
def authed_page(storage_state, browser):
    """登录态浏览器 page；每个测试独立 context，避免互相污染。"""
    ctx = browser.new_context(storage_state=storage_state)
    page = ctx.new_page()
    yield page
    ctx.close()
