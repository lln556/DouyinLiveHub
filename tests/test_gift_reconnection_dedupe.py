"""Gift accounting, reconnect replay, and safe realtime message contracts."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from models.database import GiftMessage as StoredGift, LiveSession
from services.data_service import DataService
from services.room_manager import MonitoredRoom
from ws_handlers.handlers import WebDouyinLiveFetcher


def user(user_id='stable-id', name='viewer'):
    return SimpleNamespace(
        id=123, id_str=user_id, nick_name=name,
        pay_grade=SimpleNamespace(level=12),
        fans_club=SimpleNamespace(data=SimpleNamespace(level=3)),
        gender=2, follow_info=SimpleNamespace(follower_count=40, following_count=5),
        age_range=4, avatar_thumb=SimpleNamespace(url_list_list=['https://example.test/avatar']),
    )


def gift(trace='trace-1', msg_id=1, group=0, count=1, combo=0, repeat=0, end=0, name='heart', price=2.5):
    return SimpleNamespace(
        user=user(), gift=SimpleNamespace(name=name, diamond_count=price), gift_id=8,
        trace_id=trace, common=SimpleNamespace(msg_id=msg_id), group_id=group,
        group_count=count, combo_count=combo, repeat_count=repeat, repeat_end=end, send_type=0,
    )


@pytest.fixture
def context():
    svc = DataService('sqlite:///:memory:')
    svc.create_tables()
    svc.create_live_room('room', anchor_name='anchor', status='monitoring')
    session = svc.create_live_session('room', anchor_name='anchor', status='live')
    manager = SimpleNamespace(data_service=svc)
    room = MonitoredRoom('room', manager)
    room.anchor_name = 'anchor'
    socket = MagicMock()
    with patch('crawler.DouyinLiveWebFetcher') as core:
        core.return_value.anchor_info = {}
        fetcher = WebDouyinLiveFetcher('room', svc, socket, room)
        fetcher.log = MagicMock()
        yield svc, session, room, socket, fetcher
    svc.close_session()


def reconnect(context):
    svc, _, room, socket, _ = context
    fetcher = WebDouyinLiveFetcher('room', svc, socket, room)
    fetcher.log = MagicMock()
    return fetcher


def saved_session(svc, session_id):
    with svc.get_session() as db:
        return db.get(LiveSession, session_id)


def messages(socket):
    return [call.args[1] for call in socket.emit.call_args_list if call.args[0] == 'room_room']


def test_preload_and_ws_open_restore_income_peak_and_gift_user_ids(context):
    svc, session, room, socket, fetcher = context
    svc.update_session_stats(session.id, total_income=42.75, peak_viewer_count=5000)
    svc.save_gift_message('room', live_session_id=session.id, user_id='older-giver',
                          user_name='old', gift_name='heart', gift_count=1, gift_price=1, total_value=1)
    room.remember_trace('preserved')
    room.remember_like('preserved-like')
    room.user_contributions['cached'] = {'user_name': 'cached', 'score': 1, 'gift_count': 1,
                                         'like_count': 0, 'avatar': None}
    second = reconnect(context)
    assert second.total_income == 42.75
    assert room.stats['total_income'] == 42.75
    assert second.max_viewer_count == 5000
    assert 'older-giver' in room.gift_user_ids
    with patch('ws_handlers.handlers.threading.Thread'):
        second._wsOnOpen(MagicMock())
    assert room.remember_trace('preserved') is False
    assert room.remember_like('preserved-like') is False
    assert 'cached' in room.user_contributions
    assert second.total_income == 42.75
    assert second.max_viewer_count == 5000
    second._handle_stats_message(SimpleNamespace(total=500, total_pv_for_anchor='1.2万'))
    assert saved_session(svc, session.id).peak_viewer_count == 5000


@pytest.mark.parametrize('trace,msg_id', [('replayed-trace', 10), ('', 20)])
def test_replay_across_fetchers_does_not_double_account(context, trace, msg_id):
    svc, session, room, _, fetcher = context
    first = gift(trace=trace, msg_id=msg_id, count=3)
    fetcher._handle_gift_message(first)
    second = reconnect(context)
    second._handle_gift_message(first)
    result = saved_session(svc, session.id)
    assert result.total_income == 7.5
    assert result.total_gift_count == 3
    assert svc.get_user_contribution('room', 'stable-id').total_score == 7.5
    with svc.get_session() as db:
        assert db.query(StoredGift).count() == 1
    assert room.gift_user_ids == {'stable-id'}


def test_db_trace_collision_and_failed_save_never_change_accounting(context):
    svc, session, room, socket, fetcher = context
    svc.save_gift_message('room', live_session_id=session.id, trace_id='already-saved',
                          user_id='stable-id', user_name='viewer', gift_name='heart',
                          gift_count=1, gift_price=2.5, total_value=2.5)
    fetcher._handle_gift_message(gift(trace='already-saved'))
    assert fetcher.total_income == 0
    assert saved_session(svc, session.id).total_income == 0
    assert svc.get_user_contribution('room', 'stable-id') is None
    with patch.object(svc, 'save_gift_message', return_value=None):
        fetcher._handle_gift_message(gift(trace='transient'))
    assert fetcher.total_income == 0
    assert room.stats['total_income'] == 0
    assert not room.gift_user_ids
    assert not messages(socket)
    # Failure must remain retryable, including a failed combo insertion.
    fetcher._handle_gift_message(gift(trace='transient'))
    assert fetcher.total_income == 2.5


def test_save_precedes_all_accounting(context):
    svc, session, room, _, fetcher = context
    original = svc.save_gift_message
    def saving(*args, **kwargs):
        assert fetcher.total_income == 0
        assert room.stats['total_income'] == 0
        assert not room.user_contributions
        assert saved_session(svc, session.id).total_income == 0
        return original(*args, **kwargs)
    with patch.object(svc, 'save_gift_message', side_effect=saving):
        fetcher._handle_gift_message(gift(count=4))
    assert fetcher.total_income == 10


def test_zero_group_is_normal_and_repeat_count_fallback(context):
    svc, session, _, socket, fetcher = context
    fetcher._handle_gift_message(gift(trace='one', group=0, count=0, repeat=4))
    fetcher._handle_gift_message(gift(trace='two', group=0, count=0, repeat=0))
    assert saved_session(svc, session.id).total_income == 12.5
    assert saved_session(svc, session.id).total_gift_count == 5
    payload = messages(socket)[0]
    assert payload['is_combo'] is False
    assert payload['gift_count'] == 4
    assert payload['total_value'] == 10
    assert payload['user_id'] == 'stable-id'


def test_combo_out_of_order_and_completed_replay_are_skipped(context):
    svc, session, room, socket, fetcher = context
    fetcher._handle_gift_message(gift(trace='three', group=88, count=2, combo=3))
    fetcher._handle_gift_message(gift(trace='two', group=88, count=2, combo=2))
    fetcher._handle_gift_message(gift(trace='three-again', group=88, count=2, combo=3))
    fetcher._handle_gift_message(gift(trace='four', group=88, count=2, combo=4, end=1))
    fetcher._handle_gift_message(gift(trace='late-final', group=88, count=2, combo=4, end=1))
    assert saved_session(svc, session.id).total_income == 20
    assert saved_session(svc, session.id).total_gift_count == 8
    assert svc.get_user_contribution('room', 'stable-id').total_score == 20
    payloads = messages(socket)
    assert len(payloads) == 2
    final = payloads[-1]
    assert final['is_combo'] is True
    assert final['gift_count'] == final['partial_count'] == 2
    assert final['total_gift_count'] == 8
    assert final['total_value'] == 5
    assert final['total_combo_value'] == 20
    assert final['combo_count'] == 4
    assert final['is_combo_end'] is True


def test_combo_fallback_quantity_failed_save_and_idle_cleanup(context):
    svc, session, room, _, fetcher = context
    room.combo_gifts['expired'] = {'last_count': 2, 'last_gift_count': 2, 'updated_at': 1}
    room.combo_gifts['active'] = {'last_count': 2, 'last_gift_count': 2, 'updated_at': 900}
    with patch('ws_handlers.handlers.time.time', return_value=1000):
        with patch.object(svc, 'save_gift_message', return_value=None):
            fetcher._handle_gift_message(gift(trace='combo-retry', group=8, count=0, repeat=3, combo=2))
        assert fetcher.total_income == 0
        fetcher._handle_gift_message(gift(trace='combo-retry', group=8, count=0, repeat=3, combo=2))
    assert 'expired' not in room.combo_gifts
    assert 'active' in room.combo_gifts
    assert saved_session(svc, session.id).total_gift_count == 6
    assert any(state['updated_at'] == 1000 for state in room.combo_gifts.values())


def test_chat_and_gift_payloads_have_pure_text_structured_fields(context):
    svc, session, room, socket, fetcher = context
    unsafe_user = user(name='<img src=x onerror=alert(1)>')
    msg = gift(name='<svg onload=alert(1)>')
    msg.user = unsafe_user
    fetcher._handle_gift_message(msg)
    chat = SimpleNamespace(user=unsafe_user, content='<script>alert(1)</script>')
    fetcher._handle_chat_message(chat)
    for payload in messages(socket):
        assert 'content' not in payload
        assert payload['user'] == unsafe_user.nick_name
        assert payload['user_id'] == 'stable-id'
        assert payload['user_level'] == 12
        assert payload['fans_club_level'] == 3
        assert payload['gender'] == 2
        assert payload['follower_count'] == 40
        assert payload['following_count'] == 5
        assert payload['age_range'] == 4
    assert messages(socket)[-1]['text'] == chat.content
    assert messages(socket)[-1]['is_gift_user'] is True
    # Same name must not identify a different user as a gift sender.
    fetcher._handle_chat_message(SimpleNamespace(user=user('other', unsafe_user.nick_name), content='hello'))
    assert messages(socket)[-1]['is_gift_user'] is False


def test_stats_before_session_does_not_reference_unbound_data_service(context):
    _, _, _, socket, fetcher = context
    fetcher.current_session_id = None
    fetcher.max_viewer_count = 100
    fetcher._handle_stats_message(SimpleNamespace(total=0, total_pv_for_anchor='2万'))
    payload = [call.args[1] for call in socket.emit.call_args_list if call.args[0] == 'room_room_stats'][-1]
    assert payload['current_session'] is None
    assert payload['total_user_count'] == 20000
    assert 'room_status' in payload
    assert 'room_error_message' in payload


def test_stats_throttle_applies_across_chat_gift_like_and_reconnect(context):
    _, _, room, socket, fetcher = context
    like = SimpleNamespace(user=user(), count=3, total=0, common=SimpleNamespace(msg_id=7), double_like_detail=None)
    with patch('ws_handlers.handlers.time.monotonic', return_value=100):
        fetcher._handle_chat_message(SimpleNamespace(user=user(), content='hello'))
        fetcher._handle_gift_message(gift())
        fetcher._handle_like_message(like)
        second = reconnect(context)
        second._emit_room_stats_throttled()
    stats = [call for call in socket.emit.call_args_list if call.args[0] == 'room_room_stats']
    assert len(stats) == 1
    socket.emit.reset_mock()
    with patch('ws_handlers.handlers.time.monotonic', return_value=101):
        second._emit_room_stats_throttled()
    payload = socket.emit.call_args.args[1]
    assert payload['total_income'] == 2.5
    assert payload['contributor_count'] == 1
    assert 'contributor_info' in payload and 'like_rank_list' in payload


def test_new_session_only_resets_dedupe_after_successful_creation(context):
    svc, session, room, _, fetcher = context
    room.remember_trace('old')
    room.remember_like('old')
    room.gift_user_ids.add('old')
    room.stats['total_income'] = 99
    room.user_contributions['old'] = {
        'user_name': 'old', 'score': 99, 'avatar': None, 'gift_count': 1, 'like_count': 0,
        'fans_club_level': 0, 'user_level': 0,
    }
    svc.end_live_session(session.id)
    with patch.object(svc, 'create_live_session', return_value=None), patch('ws_handlers.handlers.threading.Thread'):
        fetcher._wsOnOpen(MagicMock())
    assert room.remember_trace('old') is False
    assert room.remember_like('old') is False
    assert room.gift_user_ids == {'old'}
    with patch('ws_handlers.handlers.threading.Thread'):
        fetcher._wsOnOpen(MagicMock())
    assert room.remember_trace('old') is True
    assert room.remember_like('old') is True
    assert not room.gift_user_ids
    assert not room.user_contributions
    assert room.stats['total_income'] == fetcher.total_income == 0


@pytest.mark.parametrize('compressed', [False, True])
def test_push_frame_accepts_plain_and_gzip_payloads(context, compressed):
    import gzip
    from protobuf.douyin import PushFrame, Response
    _, _, _, _, fetcher = context
    payload = Response(need_ack=True, internal_ext='ack-value').SerializeToString()
    packet = PushFrame(log_id=17, payload=gzip.compress(payload) if compressed else payload)
    ws = MagicMock()
    fetcher._wsOnMessage(ws, packet.SerializeToString())
    fetcher.log.error.assert_not_called()
    ws.send.assert_called_once()
    ack = PushFrame().parse(ws.send.call_args.args[0])
    assert ack.log_id == 17
    assert ack.payload_type == 'ack'
    assert ack.payload == b'ack-value'


def test_slow_stats_read_cannot_publish_old_snapshot_after_new_stats(context):
    import threading
    svc, session, room, socket, fetcher = context
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    failures = []
    query_count = 0
    query_lock = threading.Lock()

    def current_session(live_id):
        nonlocal query_count
        with query_lock:
            query_count += 1
            first = query_count == 1
        if first:
            entered.set()
            assert release.wait(timeout=3)
        return session

    def emitting():
        try:
            fetcher._emit_room_stats_throttled()
        except Exception as exc:
            failures.append(exc)

    def updating():
        second_started.set()
        with room.data_lock:
            room.stats['total_income'] = 20
            session.total_income = 20
        emitting()

    # Once the read stalls, let the second emitter cross the one-second boundary.
    clock = MagicMock(side_effect=[100, 102])
    with patch('ws_handlers.handlers.time.monotonic', clock), \
         patch.object(svc, 'get_current_live_session', side_effect=current_session), \
         patch.object(svc, 'get_live_room', return_value=SimpleNamespace(status='monitoring', error_message=None)):
        first = threading.Thread(target=emitting)
        first.start()
        assert entered.wait(timeout=3)
        second = threading.Thread(target=updating)
        second.start()
        assert second_started.wait(timeout=3)
        # The old implementation publishes the newer event while the old DB read waits.
        second.join(timeout=0.1)
        release.set()
        first.join(timeout=3)
        second.join(timeout=3)
        assert not first.is_alive() and not second.is_alive()
    assert not failures
    payloads = [call.args[1] for call in socket.emit.call_args_list if call.args[0] == 'room_room_stats']
    assert [payload['total_income'] for payload in payloads] == [0, 20]
    assert all(payload['total_income'] == payload['current_session']['total_income'] for payload in payloads)


def test_contribution_failure_cannot_skip_saved_gift_session_accounting(context):
    svc, session, room, socket, fetcher = context
    with patch.object(svc, 'update_user_contribution', side_effect=RuntimeError('sensitive database detail')):
        fetcher._handle_gift_message(gift(trace='contribution-write-error', count=3))
    assert saved_session(svc, session.id).total_income == 7.5
    assert saved_session(svc, session.id).total_gift_count == 3
    assert room.stats['total_income'] == 7.5
    assert room.user_contributions['stable-id']['score'] == 7.5
    assert len(messages(socket)) == 1
    assert 'sensitive database detail' not in str(fetcher.log.error.call_args_list)
    # Replay remains harmless despite the contribution write failure.
    fetcher._handle_gift_message(gift(trace='contribution-write-error', count=3))
    assert saved_session(svc, session.id).total_income == 7.5
