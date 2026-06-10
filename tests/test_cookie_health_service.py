"""CookieHealthService 状态机单测：防抖、恢复、事件写入。probe 全部 mock。"""
import pytest

import config
from services.cookie_health import CookieHealthService, CookieProbeResult


class StubDataService:
    def __init__(self):
        self.events = []

    def log_system_event(self, live_id, event_type, message=None, data=None, anchor_name=None):
        self.events.append((live_id, event_type, message))


class StubRoomManager:
    def __init__(self):
        self.active_rooms = {}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', 'sessionid=test')
    return CookieHealthService(StubRoomManager(), StubDataService())


def _set_probe(monkeypatch, outcome, detail=''):
    monkeypatch.setattr(
        'services.cookie_health.probe_douyin_cookie',
        lambda cookie, proxies=None: CookieProbeResult(outcome, detail))


def test_initial_status_unknown_when_configured(service):
    assert service.status == 'unknown'


def test_initial_status_unconfigured_without_cookie(monkeypatch):
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', '')
    svc = CookieHealthService(StubRoomManager(), StubDataService())
    assert svc.status == 'unconfigured'


def test_alive_probe_sets_healthy(service, monkeypatch):
    _set_probe(monkeypatch, 'alive')
    snapshot = service.run_probe(trigger='manual')
    assert snapshot['status'] == 'healthy'
    assert snapshot['last_ok_time'] is not None
    assert snapshot['trigger'] == 'manual'


def test_single_dead_is_suspect_not_dead(service, monkeypatch):
    """防抖：单次明确未登录只进 suspect。"""
    _set_probe(monkeypatch, 'dead', '未检测到登录态')
    snapshot = service.run_probe(trigger='scheduled')
    assert snapshot['status'] == 'suspect'
    assert service.data_service.events == []  # 未跨界，不写事件


def test_two_consecutive_dead_marks_dead_and_logs_once(service, monkeypatch):
    _set_probe(monkeypatch, 'dead', '未检测到登录态')
    service.run_probe(trigger='scheduled')
    snapshot = service.run_probe(trigger='scheduled')
    assert snapshot['status'] == 'dead'
    dead_events = [e for e in service.data_service.events if e[1] == 'cookie_dead']
    assert len(dead_events) == 1
    assert dead_events[0][0] is None  # 全局事件 live_id=None
    # 再探测一次仍 dead，不重复写事件
    service.run_probe(trigger='scheduled')
    assert len([e for e in service.data_service.events if e[1] == 'cookie_dead']) == 1


def test_skip_debounce_marks_dead_immediately(service, monkeypatch):
    """手动测活跳过防抖：单次未登录即判 dead。"""
    _set_probe(monkeypatch, 'dead', '未检测到登录态')
    snapshot = service.run_probe(trigger='manual', skip_debounce=True)
    assert snapshot['status'] == 'dead'


def test_inconclusive_keeps_status(service, monkeypatch):
    """网络错误不可作为失活证据：状态不变，只记 last_error。"""
    _set_probe(monkeypatch, 'alive')
    service.run_probe(trigger='scheduled')
    _set_probe(monkeypatch, 'inconclusive', '网络请求失败')
    snapshot = service.run_probe(trigger='scheduled')
    assert snapshot['status'] == 'healthy'
    assert snapshot['last_error'] == '网络请求失败'


def test_recover_from_dead_logs_recovered(service, monkeypatch):
    _set_probe(monkeypatch, 'dead', 'x')
    service.run_probe(trigger='manual', skip_debounce=True)
    _set_probe(monkeypatch, 'alive')
    snapshot = service.run_probe(trigger='cookie_updated')
    assert snapshot['status'] == 'healthy'
    recovered = [e for e in service.data_service.events if e[1] == 'cookie_recovered']
    assert len(recovered) == 1


def test_alive_resets_fail_count(service, monkeypatch):
    """dead→alive→dead：fail_count 被重置，单次 dead 又回到 suspect。"""
    _set_probe(monkeypatch, 'dead', 'x')
    service.run_probe(trigger='scheduled')          # suspect, fail_count=1
    _set_probe(monkeypatch, 'alive')
    service.run_probe(trigger='scheduled')          # healthy, fail_count=0
    _set_probe(monkeypatch, 'dead', 'x')
    snapshot = service.run_probe(trigger='scheduled')
    assert snapshot['status'] == 'suspect'


def test_empty_cookie_sets_unconfigured(service, monkeypatch):
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', '')
    snapshot = service.run_probe(trigger='cookie_updated')
    assert snapshot['status'] == 'unconfigured'


# ───────── 被动信号 & tick ─────────

import time
from types import SimpleNamespace


def _room_with_fetcher(**kw):
    """构造带 fetcher 时间戳属性的假 MonitoredRoom。"""
    return SimpleNamespace(fetcher=SimpleNamespace(
        ws_open_time=kw.get('ws_open_time'),
        last_chat_time=kw.get('last_chat_time'),
        last_gift_time=kw.get('last_gift_time'),
    ))


def test_passive_signal_hits_when_chat_active_but_no_gift(service):
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 30, last_gift_time=None)
    assert service._passive_signal_hit() is True


def test_passive_signal_vetoed_by_recent_gift(service):
    """任一房间近期收到礼物 → Cookie 显然有效，信号否决。"""
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 30, last_gift_time=None)
    service.room_manager.active_rooms['r2'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 30, last_gift_time=now - 60)
    assert service._passive_signal_hit() is False


def test_passive_signal_ignores_freshly_connected_room(service):
    """连接未满 GIFT_SILENCE 的房间不能作为证据（刚连上没礼物很正常）。"""
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 60, last_chat_time=now - 30, last_gift_time=None)
    assert service._passive_signal_hit() is False


def test_passive_signal_ignores_inactive_room(service):
    """没有近期弹幕的房间（没人看/没开播）不能作为证据。"""
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 3600, last_gift_time=None)
    assert service._passive_signal_hit() is False


def test_tick_passive_hit_triggers_probe(service, monkeypatch):
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 30, last_gift_time=None)
    calls = []
    monkeypatch.setattr(service, 'run_probe', lambda trigger, **kw: calls.append(trigger))
    service.tick()
    assert calls == ['passive']


def test_tick_passive_rate_limited(service, monkeypatch):
    """距上次探测不足 15 分钟时被动信号不再触发，但定时探测条件也未到 → 无探测。"""
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 30, last_gift_time=None)
    service._last_probe_at = now - 60  # 1 分钟前刚探测过
    calls = []
    monkeypatch.setattr(service, 'run_probe', lambda trigger, **kw: calls.append(trigger))
    service.tick()
    assert calls == []


def test_tick_scheduled_probe_when_interval_elapsed(service, monkeypatch):
    service._last_probe_at = time.time() - config.COOKIE_HEALTH_CHECK_INTERVAL - 1
    calls = []
    monkeypatch.setattr(service, 'run_probe', lambda trigger, **kw: calls.append(trigger))
    service.tick()
    assert calls == ['scheduled']


def test_tick_first_run_probes_immediately(service, monkeypatch):
    """启动后首次 tick（_last_probe_at 为 None）立即做定时探测。"""
    calls = []
    monkeypatch.setattr(service, 'run_probe', lambda trigger, **kw: calls.append(trigger))
    service.tick()
    assert calls == ['scheduled']


def test_tick_unconfigured_sets_status_and_skips_probe(service, monkeypatch):
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', '')
    calls = []
    monkeypatch.setattr(service, 'run_probe', lambda trigger, **kw: calls.append(trigger))
    service.tick()
    assert calls == []
    assert service.status == 'unconfigured'
