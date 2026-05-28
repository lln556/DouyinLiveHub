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
from sqlalchemy import text  # noqa: E402

from models.database import (  # noqa: E402
    ChatMessage,
    GiftMessage,
    LiveRoom,
    LiveSession,
    UserContribution,
)
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
def storage_state(playwright, base_url, auth, tmp_path_factory):
    """登录一次复用 cookie，避免每个测试都跑一遍登录流程。

    复用 pytest-playwright 提供的 session-scope `playwright` 实例，
    不能自己 `with sync_playwright()` —— 同一进程同时存在两个 sync_playwright
    实例会被检测为 "Sync API inside asyncio loop" 并报错。
    """
    state_file = tmp_path_factory.mktemp("auth") / "state.json"
    browser = playwright.chromium.launch()
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"{base_url}/login")
        page.fill("input[name=username]", auth["username"])
        page.fill("input[name=password]", auth["password"])
        page.click("button[type=submit]")
        page.wait_for_url(f"{base_url}/")
        ctx.storage_state(path=str(state_file))
    finally:
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


@pytest.fixture
def api_client():
    """Flask test_client 包装为 Playwright APIRequestContext 风格的接口。

    用 in-process test_client 而非真 HTTP：
    - conftest 顶层已 load_dotenv .env.test，import app 时连的就是测试库
    - 不依赖 docker app 容器（容器连 dev 库，跟测试库不通）
    - 接口跟 Playwright APIRequestContext 对齐（.status / .ok / .json() / .text()），
      将来若需要切到真 HTTP，测试代码无需改动
    """
    from app import app as flask_app  # 延迟 import 确保 .env.test 已生效
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        # 注入登录态，跳过 /login 表单
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = os.environ.get("AUTH_USERNAME", "admin")
        yield _APIClientAdapter(client)


class _APIResponseAdapter:
    """模仿 Playwright APIResponse 的最小子集。"""

    def __init__(self, flask_response):
        self._r = flask_response

    @property
    def status(self) -> int:
        return self._r.status_code

    @property
    def ok(self) -> bool:
        return self._r.status_code < 400

    def json(self):
        return self._r.get_json()

    def text(self) -> str:
        return self._r.get_data(as_text=True)


class _APIClientAdapter:
    """模仿 Playwright APIRequestContext 的最小子集。"""

    def __init__(self, test_client):
        self._c = test_client

    def get(self, url, **kw):
        return _APIResponseAdapter(self._c.get(url, **kw))

    def post(self, url, *, data=None, **kw):
        if isinstance(data, dict):
            return _APIResponseAdapter(self._c.post(url, json=data, **kw))
        return _APIResponseAdapter(self._c.post(url, data=data, **kw))

    def put(self, url, *, data=None, **kw):
        if isinstance(data, dict):
            return _APIResponseAdapter(self._c.put(url, json=data, **kw))
        return _APIResponseAdapter(self._c.put(url, data=data, **kw))

    def delete(self, url, **kw):
        return _APIResponseAdapter(self._c.delete(url, **kw))


class _Factories:
    """业务对象工厂；每次 commit 让断点调试能直接 SELECT 看到。

    所有 fixture 默认值只满足 NOT NULL 与最常见用法；测试用 kwargs 覆盖。
    """

    def __init__(self, session):
        self.session = session
        self._room_seq = 0

    def room(self, *, live_id=None, anchor_name="测试主播", **kw):
        if live_id is None:
            self._room_seq += 1
            live_id = f"testroom_{self._room_seq:03d}"
        defaults = dict(
            anchor_name=anchor_name,
            status="stopped",
            monitor_type="manual",
            auto_reconnect=False,
        )
        defaults.update(kw)
        room = LiveRoom(live_id=live_id, **defaults)
        self.session.add(room)
        self.session.commit()
        return room

    def _resolve_anchor_name(self, live_id, kw):
        if "anchor_name" not in kw:
            room = self.session.query(LiveRoom).filter_by(live_id=live_id).first()
            if room:
                kw["anchor_name"] = room.anchor_name
        return kw

    def chat(self, *, live_id, user_id, user_name, content="hi", **kw):
        kw = self._resolve_anchor_name(live_id, kw)
        msg = ChatMessage(
            live_id=live_id,
            user_id=user_id,
            user_name=user_name,
            content=content,
            **kw,
        )
        self.session.add(msg)
        self.session.commit()
        return msg

    def gift(self, *, live_id, user_id, user_name,
             gift_name="玫瑰", gift_count=1, gift_price=10, **kw):
        kw = self._resolve_anchor_name(live_id, kw)
        msg = GiftMessage(
            live_id=live_id,
            user_id=user_id,
            user_name=user_name,
            gift_name=gift_name,
            gift_count=gift_count,
            gift_price=gift_price,
            total_value=gift_price * gift_count,
            send_type=kw.pop("send_type", "normal"),
            **kw,
        )
        self.session.add(msg)
        self.session.commit()
        return msg

    def session_(self, *, live_id, **kw):
        kw = self._resolve_anchor_name(live_id, kw)
        sess = LiveSession(live_id=live_id, **kw)
        self.session.add(sess)
        self.session.commit()
        return sess

    def contribution(self, *, live_id, user_id, user_name, total_score=100, **kw):
        kw = self._resolve_anchor_name(live_id, kw)
        c = UserContribution(
            live_id=live_id,
            user_id=user_id,
            user_name=user_name,
            total_score=total_score,
            **kw,
        )
        self.session.add(c)
        self.session.commit()
        return c


@pytest.fixture
def factories(data_service):
    """业务对象工厂。clean_db autouse 已经 truncate 过，这里安全插数据。"""
    sess = data_service.get_session()
    try:
        yield _Factories(sess)
    finally:
        sess.close()
