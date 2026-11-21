"""L1 集成测试：/api/rooms/stats/summary 三态分离行为。

验证 get_stats_summary 把 monitoring / waiting (offline) / stopped 严格分开，
不再把所有非 monitoring 房间都算成 stopped_rooms。
"""
import pytest

pytestmark = pytest.mark.integration


def test_empty_db_returns_all_zeros(api_client):
    resp = api_client.get("/api/rooms/stats/summary")
    assert resp.status == 200
    body = resp.json()
    assert body["total_rooms"] == 0
    assert body["monitoring_rooms"] == 0
    assert body["waiting_rooms"] == 0
    assert body["stopped_rooms"] == 0
    assert body["archived_rooms"] == 0


def test_each_status_lands_in_correct_bucket(api_client, factories):
    """监控中/等待开播/已停止/已归档各 1 个，验证不混淆。"""
    factories.room(live_id="r_mon", status="monitoring")
    factories.room(live_id="r_off", status="offline")
    factories.room(live_id="r_stop", status="stopped")
    # 已归档：archived_at 非空（不影响 active count）
    from datetime import datetime
    from models.database import CHINA_TZ
    factories.room(live_id="r_arch", status="stopped",
                   archived_at=datetime(2026, 5, 1, tzinfo=CHINA_TZ))

    resp = api_client.get("/api/rooms/stats/summary")
    assert resp.status == 200
    body = resp.json()
    # 主页可见 = 未归档 = 3（monitoring + offline + stopped）
    assert body["total_rooms"] == 3
    assert body["monitoring_rooms"] == 1
    assert body["waiting_rooms"] == 1
    assert body["stopped_rooms"] == 1
    assert body["archived_rooms"] == 1


def test_error_status_is_not_counted_as_stopped(api_client, factories):
    """status='error' 不应被算进 stopped_rooms（KPI"已停止"语义是"用户手动停止"）。

    sum 不必守恒：total_rooms 可能 > monitoring + waiting + stopped。
    这种异常房间只能从表格看到，KPI 不显示。
    """
    factories.room(live_id="r_err", status="error")
    factories.room(live_id="r_stop", status="stopped")

    resp = api_client.get("/api/rooms/stats/summary")
    body = resp.json()
    assert body["total_rooms"] == 2
    assert body["stopped_rooms"] == 1  # 不含 error
    assert body["monitoring_rooms"] == 0
    assert body["waiting_rooms"] == 0



def test_waiting_status_counts_as_waiting(api_client, factories):
    factories.room(live_id='waiting-explicit', status='waiting')
    body = api_client.get('/api/rooms/stats/summary').json()
    assert body['waiting_rooms'] == 1
    assert body['stopped_rooms'] == 0


def test_room_list_queries_live_sessions_once(api_client, factories, monkeypatch):
    import app as app_module
    from sqlalchemy import event
    for i in range(20):
        rid = f'list-{i}'
        factories.room(live_id=rid)
        if i % 2 == 0:
            factories.session_(live_id=rid, status='live')
    sql = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)
    event.listen(app_module.data_service.engine, 'before_cursor_execute', capture)
    try:
        response = api_client.get('/api/rooms')
    finally:
        event.remove(app_module.data_service.engine, 'before_cursor_execute', capture)
    assert response.status == 200
    assert sum('FROM live_sessions' in q for q in sql) == 1
    assert sum(row['live_status'] == 'live' for row in response.json()['rooms']) == 10
