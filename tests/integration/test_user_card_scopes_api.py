from datetime import datetime

import pytest

from models.database import CHINA_TZ

pytestmark = pytest.mark.integration


def test_user_messages_empty_session_and_anchor_params_return_200(api_client, factories):
    room = factories.room(live_id='room_scope_empty', anchor_name='测试主播')
    s_live = factories.session_(live_id=room.live_id, status='live', start_time=datetime(2026, 5, 10, 20, 0, tzinfo=CHINA_TZ))
    factories.chat(live_id=room.live_id, live_session_id=s_live.id, user_id='u1', user_name='用户A', content='hi')

    resp = api_client.get(
        f'/api/rooms/{room.live_id}/user-messages'
        '?user_id=u1&session_id=&anchor_session_id=&anchor_start_date=&anchor_end_date='
    )
    assert resp.status == 200
    body = resp.json()
    assert len(body['messages']) == 1
    assert body['messages'][0]['session_id'] == s_live.id
    assert body['scope_summary']['anchor_session']['id'] == s_live.id
    assert body['scope_summary']['focus']['kind'] == 'session'


@pytest.mark.parametrize('query', [
    'user_id=1&session_id=bad',
    'user_id=1&session_id=-1',
    'user_id=1&anchor_session_id=bad',
    'user_id=1&anchor_session_id=0',
    'user_id=1&anchor_start_date=2026-13-40',
    'user_id=1&anchor_end_date=bad-date',
    'user_id=1&start_date=2026/05/01',
    'user_id=1&type=invalid',
])
def test_user_messages_invalid_scope_params_return_400(api_client, query):
    resp = api_client.get(f'/api/rooms/room_invalid_scope/user-messages?{query}')
    assert resp.status == 400


def test_user_messages_anchor_params_populate_focus_without_filtering_all(api_client, factories):
    room = factories.room(live_id='room_scope_api', anchor_name='直播间主播')
    other = factories.room(live_id='room_other_api', anchor_name='其他主播')
    s_old = factories.session_(
        live_id=room.live_id, status='ended',
        start_time=datetime(2026, 5, 1, 19, 0, tzinfo=CHINA_TZ),
        end_time=datetime(2026, 5, 1, 21, 0, tzinfo=CHINA_TZ),
    )
    s_live = factories.session_(
        live_id=room.live_id, status='live',
        start_time=datetime(2026, 5, 2, 19, 0, tzinfo=CHINA_TZ),
    )
    s_other = factories.session_(
        live_id=other.live_id, status='live',
        start_time=datetime(2026, 5, 2, 20, 0, tzinfo=CHINA_TZ),
    )
    factories.chat(
        live_id=room.live_id, live_session_id=s_old.id, user_id='u_scope', user_name='双场用户',
        content='旧场消息', created_at=datetime(2026, 5, 1, 19, 30, tzinfo=CHINA_TZ),
    )
    factories.gift(
        live_id=room.live_id, live_session_id=s_live.id, user_id='u_scope', user_name='双场用户',
        gift_name='小心心', gift_count=3, gift_price=3.5,
        created_at=datetime(2026, 5, 2, 19, 30, tzinfo=CHINA_TZ),
    )
    factories.contribution(
        live_id=room.live_id, user_id='u_scope', user_name='双场用户',
        total_score=10.5, gift_count=3, chat_count=1, like_count=25, user_level=30,
    )

    resp_all = api_client.get(
        f'/api/rooms/{room.live_id}/user-messages?user_id=u_scope&anchor_session_id={s_live.id}'
    )
    assert resp_all.status == 200
    body = resp_all.json()
    assert len(body['messages']) == 2
    assert set(body['sessions'].keys()) == {str(s_old.id), str(s_live.id)}
    assert body['scope_summary']['focus']['total_messages'] == 1
    assert body['scope_summary']['focus']['total_value'] == 10.5
    assert body['scope_summary']['all']['total_messages'] == 2
    assert body['scope_summary']['all']['like_count'] == 25

    resp_leak = api_client.get(
        f'/api/rooms/{room.live_id}/user-messages?user_id=u_scope&anchor_session_id={s_other.id}'
    )
    assert resp_leak.status == 200
    assert resp_leak.json()['scope_summary']['focus'] is None
    assert str(s_other.id) not in resp_leak.json()['sessions']
