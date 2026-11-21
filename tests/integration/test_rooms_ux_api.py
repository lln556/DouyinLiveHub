"""首页实时统计与房间详情场次消息回填的 API 回归测试。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration


@pytest.fixture
def app_module(monkeypatch):
    import app as module

    assert make_url(module.config.DATABASE_URL).database == "douyin_live_test"
    monkeypatch.setattr(module, "initialize_once_async", lambda: None)
    monkeypatch.setattr(module.room_manager, "active_rooms", {})
    return module


def test_room_list_exposes_live_metrics_with_one_session_query(api_client, factories, app_module):
    expected = {}
    for index in range(20):
        live_id = f"ux-room-{index}"
        alive = index % 4 == 0
        has_session = index % 2 == 0
        monitor_type = "24h" if alive else "manual"
        factories.room(live_id=live_id, status="monitoring" if alive else "stopped",
                       monitor_type=monitor_type, auto_reconnect=alive)
        session = factories.session_(live_id=live_id, status="live" if has_session else "ended",
                                     total_income=20.125 + index, peak_viewer_count=900)
        stats = {"current_user_count": 100 + index,
                 "total_income": 30.456 if index == 0 else 2.0}
        app_module.room_manager.active_rooms[live_id] = SimpleNamespace(
            thread=SimpleNamespace(is_alive=lambda alive=alive: alive),
            get_stats=lambda stats=stats: stats)
        expected[live_id] = {
            "monitor_type": monitor_type,
            "auto_reconnect": alive,
            "current_user_count": 100 + index if alive else 0,
            "session_income": round(max(20.125 + index, stats["total_income"]), 2) if has_session else 0.0,
            "current_session_id": session.id if has_session else None,
            "live_status": "live" if has_session else "offline",
        }

    factories.room(live_id="ux-no-monitor")
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(app_module.data_service.engine, "before_cursor_execute", capture)
    try:
        response = api_client.get("/api/rooms")
    finally:
        event.remove(app_module.data_service.engine, "before_cursor_execute", capture)

    assert response.status == 200
    rows = {row["live_id"]: row for row in response.json()["rooms"]}
    assert len(rows) == 21
    for live_id, fields in expected.items():
        for field, value in fields.items():
            assert rows[live_id][field] == value
    assert rows["ux-no-monitor"]["current_user_count"] == 0
    assert rows["ux-no-monitor"]["session_income"] == 0.0
    assert rows["ux-no-monitor"]["current_session_id"] is None
    assert sum("FROM live_sessions" in statement for statement in statements) == 1
    assert sum(statement.lstrip().upper().startswith("SELECT") for statement in statements) == 2


@pytest.fixture
def session_messages(factories):
    live_id = "ux-messages"
    factories.room(live_id=live_id)
    old = factories.session_(live_id=live_id, status="ended")
    current = factories.session_(live_id=live_id, status="live")
    now = datetime(2026, 11, 21, 10, 0, 0)
    for session in (old, current):
        factories.chat(live_id=live_id, live_session_id=session.id, user_id="chat-user",
                       user_name="弹幕用户<script>", user_level=12, fans_club_level=3,
                       content=f"场次 {session.id} <img src=x onerror=alert(1)>",
                       is_gift_user=True, created_at=now)
        factories.gift(live_id=live_id, live_session_id=session.id, user_id="gift-user",
                       user_name="礼物用户", user_level=20, fans_club_level=4,
                       gift_name="玫瑰", gift_count=2, gift_price=10,
                       created_at=now + timedelta(seconds=1))
    return live_id, current.id


def test_all_messages_filter_session_and_return_structured_iso_data(api_client, session_messages, app_module):
    live_id, session_id = session_messages
    response = api_client.get(f"/api/rooms/{live_id}/messages?type=all&session_id={session_id}&limit=30")
    assert response.status == 200
    body = response.json()
    assert body["counts"] == {"chat_count": 1, "gift_count": 1, "total_count": 2}
    assert body["pagination"]["total"] == 2
    messages = body["messages"]
    assert [msg["type"] for msg in messages] == ["gift", "chat"]
    for msg in messages:
        assert msg["live_session_id"] == session_id
        assert msg["user"] == msg["user_name"]
        assert "T" in msg["created_at"]
        assert datetime.fromisoformat(msg["created_at"])
        assert "display_content" in msg
    gift, chat = messages
    assert (chat["user_id"], chat["user_level"], chat["fans_club_level"]) == ("chat-user", 12, 3)
    assert chat["text"] == f"场次 {session_id} <img src=x onerror=alert(1)>"
    assert chat["is_gift_user"] is True
    assert chat["gift_name"] is None
    assert chat["gift_count"] is None
    assert chat["total_value"] is None
    assert chat["gift_price"] is None
    assert (gift["user_id"], gift["user_level"], gift["fans_club_level"]) == ("gift-user", 20, 4)
    assert (gift["gift_name"], gift["gift_count"], gift["total_value"], gift["gift_price"]) == ("玫瑰", 2, 20.0, 10.0)
    assert gift["text"] is None
    assert gift["is_gift_user"] is False


@pytest.mark.parametrize("msg_type", ["chat", "gift"])
def test_typed_messages_filter_session_and_keep_user_fields(api_client, session_messages, app_module, msg_type):
    live_id, session_id = session_messages
    response = api_client.get(f"/api/rooms/{live_id}/messages?type={msg_type}&session_id={session_id}")
    assert response.status == 200
    body = response.json()
    assert body["pagination"]["total"] == 1
    assert len(body["messages"]) == 1
    message = body["messages"][0]
    assert message["type"] == msg_type
    assert message["live_session_id"] == session_id
    assert message["user_id"] == f"{msg_type}-user"
    assert message["user_name"]
    assert message["fans_club_level"] == (3 if msg_type == "chat" else 4)
    assert "T" in message["created_at"]
    if msg_type == "chat":
        assert message["content"].startswith(f"场次 {session_id} ")
        assert message["is_gift_user"] is True
    else:
        assert message["gift_price"] == 10.0
        assert message["total_value"] == 20.0


@pytest.mark.parametrize("session_param", ["", "&session_id="])
def test_unfiltered_all_messages_also_use_iso_timestamps(api_client, session_messages, app_module, session_param):
    live_id, _ = session_messages
    response = api_client.get(f"/api/rooms/{live_id}/messages?type=all{session_param}")
    assert response.status == 200
    messages = response.json()["messages"]
    assert len(messages) == 4
    assert all("T" in msg["created_at"] and datetime.fromisoformat(msg["created_at"]) for msg in messages)


def test_session_message_pagination_and_unknown_session(api_client, session_messages, app_module):
    live_id, session_id = session_messages
    response = api_client.get(f"/api/rooms/{live_id}/messages?session_id={session_id}&limit=1&offset=1")
    assert response.status == 200
    assert [msg["type"] for msg in response.json()["messages"]] == ["chat"]
    assert response.json()["pagination"]["total_pages"] == 2
    empty = api_client.get(f"/api/rooms/{live_id}/messages?session_id=2147483647")
    assert empty.status == 200
    assert empty.json()["messages"] == []
    assert empty.json()["pagination"]["total"] == 0


@pytest.mark.parametrize("session_id", ["abc", "0", "-1", "2147483648", "1.5"])
def test_invalid_message_session_id_returns_400(api_client, app_module, session_id):
    response = api_client.get(f"/api/rooms/ux-messages/messages?session_id={session_id}")
    assert response.status == 400
