"""L1 API 集成测试：/api/rooms/user-search 与 /api/rooms/<id>/user-search。

覆盖搜索 API 的关键契约：
- 同一用户多条消息按 (live_id, user_id) 去重为一条
- 全局搜跨房间 = 同 user_id 在不同房间 = 多条
- 房间路径下的搜索只看本房间
- 缺 user_name 返回 400
- 无匹配返回 200 + 空数组
"""
import pytest
from datetime import datetime
from urllib.parse import urlencode

from models.database import CHINA_TZ

pytestmark = pytest.mark.integration


def _search(api_client, live_id, query, **params):
    return api_client.get(f'/api/rooms/{live_id}/user-search?' +
                          urlencode({'user_name': query, **params}))


@pytest.mark.parametrize('query', ['   ', '空白昵称', '[空白昵称]', '\u200d'])
def test_blank_search_includes_messages_and_contribution_only(api_client, factories, query):
    factories.room(live_id='room_blank_test', anchor_name='测试主播')
    names = ['   ', '\u3164', '\u2800', '\u200d\u3164\u2800', '']
    for index, name in enumerate(names):
        uid = f'mock_blank_{index}'
        if index % 2:
            factories.gift(live_id='room_blank_test', user_id=uid, user_name=name)
        else:
            factories.chat(live_id='room_blank_test', user_id=uid, user_name=name)
    for uid, name in [('mock_blank_like', '\u200d'), ('mock_blank_history', '\u3164\u2800')]:
        factories.contribution(live_id='room_blank_test', user_id=uid, user_name=name,
                               like_count=7, chat_count=3, gift_count=2, total_score=20)
    factories.chat(live_id='room_blank_test', user_id='mock_visible', user_name='爱 丽 丝')
    factories.contribution(live_id='room_blank_test', user_id='mock_visible_contrib', user_name='爱 丽 丝')
    response = _search(api_client, 'room_blank_test', query)
    assert response.status == 200
    users = {row['user_id']: row for row in response.json()['users']}
    assert set(users) == {*(f'mock_blank_{i}' for i in range(5)), 'mock_blank_like', 'mock_blank_history'}
    assert users['mock_blank_history']['chat_count'] == 3
    assert users['mock_blank_history']['gift_count'] == 2
    assert users['mock_blank_history']['total_value'] == 20
    assert users['mock_blank_like']['like_count'] == 7


@pytest.mark.parametrize('query', ['1000000099', 'mock_dy_01', 'ID: 1000000099',
                                 'id：1000000099', '抖音号: mock_dy_01', '抖音号： mock_dy_01'])
def test_uid_and_display_id_search_preserves_session_and_date_filters(api_client, factories, query):
    factories.room(live_id='room_id_test', anchor_name='测试主播')
    old = datetime(2026, 1, 1, 12, tzinfo=CHINA_TZ)
    current = datetime(2026, 1, 2, 12, tzinfo=CHINA_TZ)
    first = factories.session_(live_id='room_id_test', start_time=old)
    second = factories.session_(live_id='room_id_test', start_time=current)
    empty = factories.session_(live_id='room_id_test', start_time=current)
    factories.contribution(live_id='room_id_test', user_id='1000000099', user_name='测试观众',
                           display_id='mock_dy_01', total_score=999, updated_at=current)
    factories.contribution(live_id='room_id_test', user_id='mock_only_like', user_name='测试点赞观众',
                           display_id='mock_dy_01', like_count=10, total_score=777, updated_at=current)
    for sess, at in [(first, old), (second, current)]:
        factories.chat(live_id='room_id_test', user_id='1000000099', user_name='测试观众',
                       live_session_id=sess.id, created_at=at)
        factories.gift(live_id='room_id_test', user_id='1000000099', user_name='测试观众',
                       live_session_id=sess.id, created_at=at, gift_count=2, gift_price=3)
    for filters in ({'session_id': second.id}, {'start_date': '2026-01-02', 'end_date': '2026-01-02'}):
        response = _search(api_client, 'room_id_test', query, **filters)
        assert response.status == 200
        users = {row['user_id']: row for row in response.json()['users']}
        assert '1000000099' in users
        user = users['1000000099']
        assert (user['chat_count'], user['gift_count'], user['total_messages'], user['total_value']) == (1, 2, 2, 6)
        assert user['display_id'] == 'mock_dy_01'
        if 'session_id' in filters:
            assert set(users) == {'1000000099'}
        elif 'mock_dy_01' in query:
            assert users['mock_only_like']['total_value'] == 0
    for filters in ({'session_id': empty.id}, {'end_date': '2025-12-31'}, {'start_date': '2026-01-03'}):
        response = _search(api_client, 'room_id_test', query, **filters)
        assert response.status == 200
        assert response.json()['users'] == []


def test_display_id_upsert_preserves_and_returns_profile_mysql(api_client, factories, data_service):
    factories.room(live_id='room_display_mysql', anchor_name='测试主播')
    for value, expected in [(' mock_dy_01 ', 'mock_dy_01'), ('', 'mock_dy_01'),
                            (None, 'mock_dy_01'), ('   ', 'mock_dy_01'), ('mock_dy_02', 'mock_dy_02')]:
        row = data_service.update_user_contribution('room_display_mysql', '测试主播', '1000000099',
                                                   '测试观众', like_count=1, display_id=value)
        assert row.display_id == expected
    response = _search(api_client, 'room_display_mysql', 'mock_dy_02')
    assert response.status == 200
    assert response.json()['users'][0]['display_id'] == 'mock_dy_02'
    assert data_service.get_user_messages(live_id='room_display_mysql', user_id='1000000099')['user']['display_id'] == 'mock_dy_02'


def test_blank_contribution_date_window_does_not_reuse_historical_totals(api_client, factories):
    factories.room(live_id='room_blank_date_test', anchor_name='测试主播')
    at = datetime(2026, 1, 2, 12, tzinfo=CHINA_TZ)
    factories.chat(live_id='room_blank_date_test', user_id='mock_chat', user_name=' ', created_at=at)
    factories.contribution(live_id='room_blank_date_test', user_id='mock_like', user_name='\u3164',
                           updated_at=at, like_count=8, chat_count=10, gift_count=20, total_score=300)
    factories.contribution(live_id='room_blank_date_test', user_id='mock_old', user_name='\u2800',
                           updated_at=datetime(2026, 1, 1, tzinfo=CHINA_TZ))
    response = _search(api_client, 'room_blank_date_test', ' ', start_date='2026-01-02', end_date='2026-01-02')
    assert response.status == 200
    users = {row['user_id']: row for row in response.json()['users']}
    assert set(users) == {'mock_chat', 'mock_like'}
    assert (users['mock_like']['chat_count'], users['mock_like']['gift_count'], users['mock_like']['total_value']) == (0, 0, 0)
    assert users['mock_like']['like_count'] == 8


@pytest.mark.parametrize('param', ['user_name', 'username', 'keyword'])
def test_api_preserves_space_query_aliases_and_limits_raw_length(api_client, factories, param):
    factories.room(live_id='room_blank_alias_test', anchor_name='测试主播')
    factories.chat(live_id='room_blank_alias_test', user_id='mock_blank', user_name=' ')
    response = api_client.get('/api/rooms/room_blank_alias_test/user-search?' + urlencode({param: '   '}))
    assert response.status == 200
    assert response.json()['users'][0]['user_id'] == 'mock_blank'
    response = api_client.get('/api/rooms/user-search?' + urlencode({param: ' ' * 101}))
    assert response.status == 400
    assert response.json()['error'] == '用户名过长'


@pytest.mark.parametrize('profiles', [('mock_dy_01', 'mock_dy_02'), ('mock_dy_01', None)])
def test_related_rooms_merge_latest_nonempty_display_id(api_client, factories, profiles):
    for rid in ('room_mock_old', 'room_mock_new'):
        factories.room(live_id=rid, anchor_name='测试主播', anchor_id='mock_anchor_01')
    for index, (rid, display_id) in enumerate(zip(('room_mock_old', 'room_mock_new'), profiles)):
        at = datetime(2026, 1, index + 1, 12, tzinfo=CHINA_TZ)
        factories.contribution(live_id=rid, user_id='1000000099', user_name='测试合并观众',
                               display_id=display_id, updated_at=at, like_count=2, total_score=5)
        factories.chat(live_id=rid, user_id='1000000099', user_name='测试合并观众', created_at=at)
    response = _search(api_client, 'room_mock_new', '1000000099')
    assert response.status == 200
    users = response.json()['users']
    assert len(users) == 1
    assert (users[0]['chat_count'], users[0]['like_count']) == (2, 4)
    assert users[0]['display_id'] == (profiles[1] or profiles[0])


def test_display_search_respects_room_scope_and_literal_wildcards(api_client, factories):
    for rid in ('room_mock_target', 'room_mock_other'):
        factories.room(live_id=rid, anchor_name='测试主播')
    factories.contribution(live_id='room_mock_other', user_id='1000000099',
                           user_name='测试观众', display_id='mock_dy_01')
    factories.chat(live_id='room_mock_target', user_id='1000000099', user_name='测试观众')
    assert _search(api_client, 'room_mock_target', 'mock_dy_01').json()['users'] == []
    factories.contribution(live_id='room_mock_target', user_id='mock_literal', user_name='测试字面值',
                           display_id='mock_dy_%_01')
    factories.contribution(live_id='room_mock_target', user_id='mock_unrelated', user_name='测试不匹配',
                           display_id='mock_dy_xyz01')
    response = _search(api_client, 'room_mock_target', '抖音号: mock_dy_%_01')
    assert response.status == 200
    assert [row['user_id'] for row in response.json()['users']] == ['mock_literal']


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
    assert user["gift_count"] == 104
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


@pytest.mark.parametrize('keyword', ['%', '_', '\\'])
def test_search_escapes_literal_wildcards(api_client, factories, keyword):
    from urllib.parse import quote
    factories.room(live_id='literal-search')
    factories.chat(live_id='literal-search', user_id='exact', user_name='literal' + keyword, content='hi')
    factories.chat(live_id='literal-search', user_id='other', user_name='unrelated', content='hi')
    response = api_client.get('/api/rooms/literal-search/user-search?user_name=' + quote(keyword, safe=''))
    assert response.status == 200
    assert [row['user_id'] for row in response.json()['users']] == ['exact']


def test_contributor_page_queries_are_bounded_mysql(api_client, factories, data_service):
    from sqlalchemy import event
    factories.room(live_id='query-bounded')
    for i in range(20):
        factories.gift(live_id='query-bounded', user_id=f'u{i}', user_name='test', gift_count=4)
        factories.chat(live_id='query-bounded', user_id=f'u{i}', user_name='test')
    sql = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)
    event.listen(data_service.engine, 'before_cursor_execute', capture)
    try:
        result = data_service.get_contributors_by_date_range('query-bounded', page_size=20)
    finally:
        event.remove(data_service.engine, 'before_cursor_execute', capture)
    assert len(sql) <= 8
    assert all(r['gift_count'] == 4 for r in result['contributors'])
