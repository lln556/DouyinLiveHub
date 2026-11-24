from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from models.database import CHINA_TZ
from services import room_manager as room_manager_module
from services.data_service import DataService
from ws_handlers.handlers import WebDouyinLiveFetcher


@pytest.fixture(autouse=True)
def clean_db():
    return


@pytest.fixture
def svc():
    service = DataService('sqlite:///:memory:')
    service.create_tables()
    try:
        yield service
    finally:
        service.drop_tables()
        service.close_session()


def test_session_focus_and_all_scope_summary(svc):
    svc.create_live_room('room-1', anchor_name='星海主播')
    s_old = svc.create_live_session(
        'room-1', anchor_name='星海主播', status='ended',
        start_time=datetime(2026, 5, 1, 20, 0, tzinfo=CHINA_TZ),
        end_time=datetime(2026, 5, 1, 22, 0, tzinfo=CHINA_TZ),
    )
    s_live = svc.create_live_session(
        'room-1', anchor_name='星海主播', status='live',
        start_time=datetime(2026, 5, 2, 20, 0, tzinfo=CHINA_TZ),
    )

    svc.save_chat_message(
        'room-1', live_session_id=s_old.id, anchor_name='星海主播',
        user_id='u1', user_name='老粉', user_level=18, content='历史弹幕',
        created_at=datetime(2026, 5, 1, 20, 10, tzinfo=CHINA_TZ),
    )
    svc.save_gift_message(
        'room-1', live_session_id=s_old.id, anchor_name='星海主播',
        user_id='u1', user_name='老粉', user_level=18,
        gift_name='棒棒糖', gift_count=3, gift_price=3.5, total_value=10.5,
        created_at=datetime(2026, 5, 1, 20, 15, tzinfo=CHINA_TZ),
    )
    svc.save_chat_message(
        'room-1', live_session_id=s_live.id, anchor_name='星海主播',
        user_id='u1', user_name='老粉', user_level=20, content='本场弹幕',
        created_at=datetime(2026, 5, 2, 20, 5, tzinfo=CHINA_TZ),
    )
    svc.save_gift_message(
        'room-1', live_session_id=s_live.id, anchor_name='星海主播',
        user_id='u1', user_name='老粉', user_level=20,
        gift_name='玫瑰', gift_count=2, gift_price=5.25, total_value=10.5,
        created_at=datetime(2026, 5, 2, 20, 6, tzinfo=CHINA_TZ),
    )
    svc.update_user_contribution(
        'room-1', '星海主播', 'u1', '老粉',
        gift_value=21.0, gift_count=5, chat_count=2, like_count=42, user_level=25,
    )

    focused = svc.get_user_messages(live_id='room-1', user_id='u1', session_id=s_live.id)
    assert set(focused['stats'].keys()) == {'total_messages', 'chat_count', 'gift_count', 'like_count', 'total_value'}
    assert focused['stats']['like_count'] == 0
    assert focused['stats']['total_messages'] == 2
    assert focused['user']['anchor_name'] == '星海主播'
    assert focused['user']['level'] == 25
    assert [m['session_id'] for m in focused['messages']] == [s_live.id, s_live.id]
    assert str(s_live.id) in focused['sessions']
    assert focused['sessions'][str(s_live.id)]['status'] == 'live'

    summary = focused['scope_summary']
    assert summary['anchor_session']['id'] == s_live.id
    assert summary['focus']['kind'] == 'session'
    assert summary['focus']['session_id'] == s_live.id
    assert summary['focus']['chat_count'] == 1
    assert summary['focus']['gift_count'] == 1
    assert summary['focus']['gift_item_count'] == 2
    assert summary['focus']['total_value'] == 10.5
    assert summary['focus']['total_messages'] == 2

    assert summary['all']['chat_count'] == 2
    assert summary['all']['gift_count'] == 2
    assert summary['all']['gift_item_count'] == 5
    assert summary['all']['total_value'] == 21.0
    assert summary['all']['total_messages'] == 4
    assert summary['all']['like_count'] == 42
    assert summary['all']['session_count'] == 2
    assert summary['room_like_count'] == 42
    assert summary['room'] == {'live_id': 'room-1', 'anchor_name': '星海主播', 'related_live_ids': ['room-1']}

    all_view = svc.get_user_messages(live_id='room-1', user_id='u1', anchor_session_id=s_live.id)
    assert len(all_view['messages']) == 4
    assert {m['session_id'] for m in all_view['messages']} == {s_old.id, s_live.id}
    assert {str(s_old.id), str(s_live.id)}.issubset(all_view['sessions'].keys())
    assert all_view['scope_summary']['focus']['total_messages'] == 2
    assert all_view['scope_summary']['all']['total_messages'] == 4
    assert all_view['stats']['like_count'] == 42


def test_date_range_focus_and_anchor_does_not_filter_messages(svc):
    svc.create_live_room('room-1', anchor_name='日期主播')
    svc.save_chat_message(
        'room-1', user_id='u2', user_name='小王', content='早于范围',
        created_at=datetime(2026, 4, 20, 12, 0, tzinfo=CHINA_TZ),
    )
    svc.save_gift_message(
        'room-1', user_id='u2', user_name='小王', gift_name='奶茶',
        gift_count=4, gift_price=2.5, total_value=10.0,
        created_at=datetime(2026, 5, 5, 12, 0, tzinfo=CHINA_TZ),
    )

    res = svc.get_user_messages(
        live_id='room-1',
        user_id='u2',
        anchor_start_date='2026-05-01',
        anchor_end_date='2026-05-07',
    )
    assert len(res['messages']) == 2
    assert res['pagination']['total'] == 2
    assert res['scope_summary']['anchor_session'] is None
    assert res['scope_summary']['focus'] == {
        'kind': 'date_range',
        'session_id': None,
        'start_date': '2026-05-01',
        'end_date': '2026-05-07',
        'chat_count': 0,
        'gift_count': 1,
        'gift_item_count': 4,
        'total_value': 10.0,
        'total_messages': 1,
    }
    assert res['scope_summary']['all']['total_messages'] == 2


def test_level_fallback_on_empty_session_and_pruned_related_rooms(svc):
    svc.create_live_room('room-a', anchor_name='主播A', anchor_id='anchor-x')
    svc.create_live_room('room-b', anchor_name='主播A', anchor_id='anchor-x')
    s_live = svc.create_live_session('room-b', anchor_name='主播A', status='live')

    svc.update_user_contribution(
        'room-a', '主播A', 'u_pruned', '老大哥',
        gift_value=100.5, gift_count=2, chat_count=3, like_count=10, user_level=38,
    )
    svc.update_user_contribution(
        'room-b', '主播A', 'u_pruned', '老大哥',
        gift_value=50.0, gift_count=1, chat_count=4, like_count=5, user_level=0,
    )

    focused_empty = svc.get_user_messages(live_id='room-b', user_id='u_pruned', session_id=s_live.id)
    assert focused_empty['user']['level'] == 38
    assert focused_empty['stats'] == {
        'total_messages': 0, 'chat_count': 0, 'gift_count': 0, 'like_count': 0, 'total_value': 0,
    }
    assert focused_empty['scope_summary']['all']['has_pruned_history'] is True
    assert focused_empty['scope_summary']['all']['chat_count'] == 7
    assert focused_empty['scope_summary']['all']['gift_count'] == 3
    assert focused_empty['scope_summary']['all']['total_value'] == 150.5
    assert focused_empty['scope_summary']['all']['like_count'] == 15

    all_pruned = svc.get_user_messages(live_id='room-b', user_id='u_pruned')
    assert all_pruned['stats']['chat_count'] == 7
    assert all_pruned['stats']['gift_count'] == 3
    assert all_pruned['stats']['like_count'] == 15
    assert all_pruned['stats']['total_value'] == 150
    assert all_pruned['messages'] == []
    assert all_pruned['scope_summary']['all']['has_pruned_history'] is True


def test_cross_room_anchor_rejected_and_global_does_not_autodetect(svc):
    svc.create_live_room('room-a', anchor_name='甲')
    svc.create_live_room('room-b', anchor_name='乙')
    s_b = svc.create_live_session('room-b', anchor_name='乙', status='live')
    svc.save_chat_message('room-a', user_id='u1', user_name='观众', content='甲房消息')

    leaked = svc.get_user_messages(live_id='room-a', user_id='u1', anchor_session_id=s_b.id)
    assert leaked['scope_summary']['anchor_session'] is None
    assert leaked['scope_summary']['focus'] is None
    assert str(s_b.id) not in leaked['sessions']

    global_res = svc.get_user_messages(live_id=None, user_id='u1')
    assert global_res['scope_summary']['anchor_session'] is None
    assert global_res['scope_summary']['focus'] is None
    assert global_res['scope_summary']['room'] is None


def test_same_second_chat_and_gift_deterministic_tie_breaking(svc):
    svc.create_live_room('room-tie', anchor_name='同秒主播')
    s1 = svc.create_live_session('room-tie', status='ended')
    s2 = svc.create_live_session('room-tie', status='live')
    ts = datetime(2026, 5, 10, 21, 0, 0, tzinfo=CHINA_TZ)
    c1 = svc.save_chat_message('room-tie', live_session_id=s1.id, user_id='u', user_name='用户', content='c1', created_at=ts)
    g1 = svc.save_gift_message('room-tie', live_session_id=s1.id, user_id='u', user_name='用户', gift_name='花', gift_count=1, gift_price=1, total_value=1, created_at=ts)
    c2 = svc.save_chat_message('room-tie', live_session_id=s2.id, user_id='u', user_name='用户', content='c2', created_at=ts)
    g2 = svc.save_gift_message('room-tie', live_session_id=s2.id, user_id='u', user_name='用户', gift_name='钻', gift_count=1, gift_price=10, total_value=10, created_at=ts)

    res = svc.get_user_messages(live_id='room-tie', user_id='u')
    order = [(m['session_id'], m['type'], m['id']) for m in res['messages']]
    assert order == [
        (s2.id, 'gift', g2.id),
        (s2.id, 'chat', c2.id),
        (s1.id, 'gift', g1.id),
        (s1.id, 'chat', c1.id),
    ]


@pytest.mark.parametrize('kind', ['chat', 'gift', 'combo', 'series'])
def test_ws_handlers_emit_session_id_in_all_paths(svc, kind):
    from protobuf import douyin as protocol

    svc.create_live_room('room_emit', anchor_name='实时主播')
    live_session = svc.create_live_session('room_emit', anchor_name='实时主播', status='live')
    room = room_manager_module.MonitoredRoom('room_emit', SimpleNamespace(data_service=svc))
    room.anchor_name = '实时主播'
    fake_socketio = Mock()
    with patch('crawler.DouyinLiveWebFetcher'):
        fetcher = WebDouyinLiveFetcher('room_emit', svc, fake_socketio, room)
        fetcher.log = Mock()

    assert fetcher.current_session_id == live_session.id
    user = protocol.User(id=9001, id_str='9001', nick_name='实时观众')
    if kind == 'chat':
        fetcher._handle_chat_message(protocol.ChatMessage(user=user, content='你好'))
    else:
        gift = SimpleNamespace(
            user=user,
            gift=SimpleNamespace(name='鲜花', diamond_count=5),
            gift_id=501,
            trace_id=f'trace_{kind}',
            group_count=1,
            group_id=99 if kind == 'combo' else 0,
            combo_count=2 if kind == 'combo' else 0,
        )
        if kind == 'series':
            gift.series_gift_data = [
                SimpleNamespace(gift_struct=SimpleNamespace(id=502, name='子礼物', diamond_count=7))
            ]
        fetcher._handle_gift_message(gift)

    room_emits = [
        call.args[1]
        for call in fake_socketio.emit.call_args_list
        if call.args and call.args[0] == 'room_room_emit'
    ]
    assert len(room_emits) == 1
    assert room_emits[0]['session_id'] == live_session.id
