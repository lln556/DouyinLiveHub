"""Atomic writes and retention safeguards, exercised against both supported dialects."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import event, inspect

from models.database import ChatMessage, GiftMessage, LiveSession, get_china_now
from services.data_service import DataService


@pytest.fixture(params=['sqlite', 'mysql'])
def svc(request, tmp_path, data_service):
    if request.param == 'mysql':
        ds = data_service
    else:
        ds = DataService(f'sqlite:///{tmp_path / "atomic.db"}')
        ds.create_tables()
    ds.create_live_room('atomic-room', anchor_name='test')
    yield ds
    if request.param == 'sqlite':
        ds.close_session()


def test_indexes_and_duplicate_room_fallback(svc):
    expected = {'live_rooms': [('anchor_id',)], 'chat_messages': [('live_session_id', 'created_at'), ('user_id', 'created_at')], 'gift_messages': [('live_session_id', 'created_at')], 'user_contributions': [('total_score',), ('live_id', 'like_count')]}
    for table, columns in expected.items():
        found = {tuple(i['column_names']) for i in inspect(svc.engine).get_indexes(table)}
        assert set(columns) <= found
    assert svc.create_live_room('atomic-room', anchor_name='duplicate').live_id == 'atomic-room'


def test_peak_never_decreases(svc):
    live = svc.create_live_session('atomic-room', peak_viewer_count=5000)
    assert svc.update_session_stats(live.id, peak_viewer_count=500)
    assert svc.get_current_live_session('atomic-room').peak_viewer_count == 5000


def test_concurrent_atomic_session_and_contribution_updates(svc):
    live = svc.create_live_session('atomic-room')
    def add(_):
        assert svc.increment_session_stats(live.id, income_delta=.25, gift_count_delta=2, chat_count_delta=1, like_count_delta=3)
        row = svc.update_user_contribution('atomic-room', 'test', 'u', 'user', gift_value=.25, gift_count=2, chat_count=1, like_count=3)
        assert row is not None
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(add, range(30)))
    current = svc.get_current_live_session('atomic-room')
    assert (current.total_income, current.total_gift_count, current.total_chat_count, current.total_like_count) == (7.5, 60, 30, 90)
    row = svc.get_user_contribution('atomic-room', 'u')
    assert (row.total_score, row.gift_count, row.chat_count, row.like_count) == (7.5, 60, 30, 90)


def test_increment_is_one_update_without_select(svc):
    live = svc.create_live_session('atomic-room')
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(svc.engine, 'before_cursor_execute', capture)
    try:
        assert svc.increment_session_stats(live.id, income_delta=.1)
    finally:
        event.remove(svc.engine, 'before_cursor_execute', capture)
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith('UPDATE')


def test_end_preserves_fraction_and_reconciles_chat(svc):
    live = svc.create_live_session('atomic-room', total_chat_count=99)
    svc.save_gift_message('atomic-room', live_session_id=live.id, user_id='u', user_name='user', gift_name='gift', gift_count=3, gift_price=.25, total_value=.75)
    svc.save_chat_message('atomic-room', live_session_id=live.id, user_id='u', user_name='user', content='hi')
    assert svc.end_live_session(live.id)
    row = svc.get_live_sessions('atomic-room')[0]
    assert (row.total_income, row.total_gift_count, row.total_chat_count) == (.75, 3, 1)
    db = svc.get_session()
    try:
        db.query(GiftMessage).delete()
        db.query(ChatMessage).delete()
        db.commit()
    finally:
        db.close()
    assert svc.end_live_session(live.id)
    row = svc.get_live_sessions('atomic-room')[0]
    assert (row.total_income, row.total_gift_count, row.total_chat_count) == (.75, 3, 1)


def test_retained_aggregate_is_not_zeroed_after_details_expire(svc):
    old = get_china_now() - timedelta(days=100)
    live = svc.create_live_session('atomic-room', start_time=old, total_income=12.5, total_gift_count=5, total_chat_count=8)
    assert svc.end_live_session(live.id)
    row = svc.get_live_sessions('atomic-room')[0]
    assert (row.total_income, row.total_gift_count, row.total_chat_count) == (12.5, 5, 8)


def test_cleanup_preserves_live_messages_and_deletes_old_orphans(svc):
    old = get_china_now() - timedelta(days=100)
    live = svc.create_live_session('atomic-room', start_time=old)
    for sid in [live.id, None]:
        svc.save_chat_message('atomic-room', live_session_id=sid, user_id='u', user_name='user', content='hi', created_at=old)
        svc.save_gift_message('atomic-room', live_session_id=sid, user_id='u', user_name='user', gift_name='gift', gift_count=1, gift_price=1, total_value=1, created_at=old)
    result = svc.cleanup_old_data(90)
    assert result['chat_messages_deleted'] == result['gift_messages_deleted'] == 1
    assert svc.cleanup_old_data(0).get('message')


def test_stale_session_uses_last_activity_and_skips_recently_active(svc):
    now = get_china_now().replace(microsecond=0)
    old = now - timedelta(days=3)
    live = svc.create_live_session('atomic-room', start_time=old, updated_at=old)
    last = old + timedelta(hours=5)
    svc.save_chat_message('atomic-room', live_session_id=live.id, user_id='u', user_name='user', content='hi', created_at=last)
    assert svc.cleanup_stale_live_sessions() == 1
    row = svc.get_live_sessions('atomic-room')[0]
    assert row.end_time.replace(tzinfo=None) == last.replace(tzinfo=None)
    assert row.total_chat_count == 1
    svc.create_live_session('atomic-room', start_time=old, updated_at=now)
    assert svc.cleanup_stale_live_sessions() == 0


def test_archived_income_keeps_retained_history_and_query_count_constant(svc):
    for i in range(8):
        rid = f'archive-{i}'
        svc.create_live_room(rid, anchor_name='test', status='archived')
        svc.create_live_session(rid, status='ended', total_income=100)
        svc.save_gift_message(rid, user_id='u', user_name='user', gift_name='gift', gift_count=1, gift_price=1, total_value=1)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(svc.engine, 'before_cursor_execute', capture)
    try:
        rows = svc.get_archived_rooms_summary()
    finally:
        event.remove(svc.engine, 'before_cursor_execute', capture)
    assert len(statements) <= 5
    assert all(row['total_income'] == 100 for row in rows)



def test_cleanup_uses_bounded_batches(svc):
    from sqlalchemy import insert
    old = get_china_now() - timedelta(days=100)
    with svc.engine.begin() as conn:
        conn.execute(insert(ChatMessage), [dict(live_id='atomic-room', user_id='u', user_name='user', content='hi', created_at=old) for _ in range(10001)])
    selected_limits = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if 'SELECT chat_messages.id' in statement:
            selected_limits.append(parameters)
    event.listen(svc.engine, 'before_cursor_execute', capture)
    try:
        result = svc.cleanup_old_data(90)
    finally:
        event.remove(svc.engine, 'before_cursor_execute', capture)
    assert result['chat_messages_deleted'] == 10001
    assert len(selected_limits) == 3


def test_stale_expired_session_keeps_historical_aggregates(svc):
    old = get_china_now() - timedelta(days=100)
    live = svc.create_live_session('atomic-room', start_time=old, updated_at=old, total_income=999.25, total_gift_count=100, total_chat_count=50)
    # Retention can leave just a late piece of detail for a very long session.
    svc.save_gift_message('atomic-room', live_session_id=live.id, user_id='u', user_name='user', gift_name='gift', gift_count=1, gift_price=1, total_value=1, created_at=get_china_now() - timedelta(days=2))
    assert svc.cleanup_stale_live_sessions() == 1
    row = svc.get_live_sessions('atomic-room')[0]
    assert (row.total_income, row.total_gift_count, row.total_chat_count) == (999.25, 100, 50)


def test_new_session_snapshot_does_not_keep_old_stale_session_live(svc):
    from models.database import RoomStats
    now = get_china_now().replace(microsecond=0)
    old = now - timedelta(days=3)
    first = svc.create_live_session('atomic-room', start_time=old, updated_at=old)
    with svc.get_session() as db:
        db.add(LiveSession(live_id='atomic-room', start_time=now - timedelta(hours=1), updated_at=now, status='live'))
        db.add(RoomStats(live_id='atomic-room', stats_at=now, current_user_count=100))
        db.commit()
    assert svc.cleanup_stale_live_sessions() == 1
    with svc.get_session() as db:
        assert db.get(LiveSession, first.id).status == 'ended'
        assert db.query(LiveSession).filter_by(status='live').count() == 1
