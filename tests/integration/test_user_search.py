"""L1 API 集成测试：/api/rooms/user-search 与 /api/rooms/<id>/user-search。

覆盖搜索 API 的关键契约：
- 同一用户多条消息按 (live_id, user_id) 去重为一条
- 全局搜跨房间 = 同 user_id 在不同房间 = 多条
- 房间路径下的搜索只看本房间
- 缺 user_name 返回 400
- 无匹配返回 200 + 空数组
"""
import pytest

pytestmark = pytest.mark.integration


def test_same_user_collapses_chat_and_gift_into_one_row(api_client, factories):
    """同一用户的 3 条弹幕 + 2 条礼物 → API 返回 1 条聚合记录。"""
    room = factories.room(live_id="testroom_search_1", anchor_name="厨艺主播")
    for txt in ["你好主播", "好香啊", "请教个问题"]:
        factories.chat(live_id=room.live_id, user_id="u_aggr", user_name="张三", content=txt)
    factories.gift(live_id=room.live_id, user_id="u_aggr", user_name="张三",
                   gift_name="玫瑰", gift_count=5, gift_price=10)
    factories.gift(live_id=room.live_id, user_id="u_aggr", user_name="张三",
                   gift_name="小心心", gift_count=99, gift_price=1)

    resp = api_client.get(f"/api/rooms/{room.live_id}/user-search?user_name=张三")
    assert resp.status == 200

    body = resp.json()
    assert body["total"] == 1
    user = body["users"][0]
    assert user["user_id"] == "u_aggr"
    assert user["chat_count"] == 3
    assert user["gift_count"] == 2
    assert user["total_messages"] == 5
    assert user["total_value"] == 5 * 10 + 99 * 1  # 50 + 99 = 149
    assert user["nickname"] == "张三"
    assert user["anchor_name"] == "厨艺主播"


def test_global_search_returns_one_row_per_room_for_same_user(api_client, factories):
    """全局搜索：同一 user_id 在 2 个房间 = 2 条（key 是 live_id+user_id 而非 user_id）。"""
    factories.room(live_id="room_alpha", anchor_name="主播α")
    factories.room(live_id="room_beta", anchor_name="主播β")
    factories.chat(live_id="room_alpha", user_id="u_cross", user_name="跨房常客", content="在 α 房间")
    factories.chat(live_id="room_beta", user_id="u_cross", user_name="跨房常客", content="在 β 房间")

    resp = api_client.get("/api/rooms/user-search?user_name=跨房")
    assert resp.status == 200

    body = resp.json()
    assert body["total"] == 2
    pairs = {(u["live_id"], u["user_id"]) for u in body["users"]}
    assert pairs == {("room_alpha", "u_cross"), ("room_beta", "u_cross")}


def test_room_scoped_search_excludes_other_rooms(api_client, factories):
    """/api/rooms/<id>/user-search 只看 <id> 内的消息。"""
    factories.room(live_id="room_target")
    factories.room(live_id="room_other")
    factories.chat(live_id="room_target", user_id="u_z", user_name="同名用户", content="target")
    factories.chat(live_id="room_other", user_id="u_z", user_name="同名用户", content="other")

    resp = api_client.get("/api/rooms/room_target/user-search?user_name=同名用户")
    assert resp.status == 200

    body = resp.json()
    assert body["total"] == 1
    assert body["users"][0]["live_id"] == "room_target"


def test_missing_user_name_returns_400(api_client):
    """缺少 user_name 参数返回 400 + 友好错误信息。"""
    resp = api_client.get("/api/rooms/user-search")
    assert resp.status == 400
    assert "user_name" in resp.json()["error"]


def test_no_match_returns_empty_list(api_client, factories):
    """搜不到匹配用户返回 200 + users=[] + total=0。"""
    factories.room(live_id="room_empty")
    factories.chat(live_id="room_empty", user_id="u_a", user_name="爱丽丝", content="嗨")

    resp = api_client.get("/api/rooms/user-search?user_name=不存在的人")
    assert resp.status == 200

    body = resp.json()
    assert body["total"] == 0
    assert body["users"] == []
