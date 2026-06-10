"""L1 集成测试：Cookie 测活 API。

probe 网络请求一律 monkeypatch 掉；app.cookie_health_service 是模块级单例，
fixture 负责每个测试前重置其内存状态、测试后恢复 config.DOUYIN_COOKIE。
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def health_service():
    import config
    from app import cookie_health_service

    original_cookie = config.DOUYIN_COOKIE
    cookie_health_service.status = 'unknown' if config.DOUYIN_COOKIE else 'unconfigured'
    cookie_health_service.fail_count = 0
    cookie_health_service.last_check_time = None
    cookie_health_service.last_ok_time = None
    cookie_health_service.last_error = None
    cookie_health_service.last_trigger = None
    cookie_health_service._last_probe_at = None
    yield cookie_health_service
    config.DOUYIN_COOKIE = original_cookie


def _set_probe(monkeypatch, outcome, detail=''):
    from services.cookie_health import CookieProbeResult
    monkeypatch.setattr(
        'services.cookie_health.probe_douyin_cookie',
        lambda cookie, proxies=None: CookieProbeResult(outcome, detail))


def test_get_cookie_config_includes_health(api_client, health_service):
    resp = api_client.get('/api/douyin-cookie')
    assert resp.status == 200
    body = resp.json()
    assert 'health' in body
    assert body['health']['status'] in ('unconfigured', 'unknown', 'healthy', 'suspect', 'dead')


def test_manual_check_dead_marks_dead_and_logs_event(api_client, health_service, monkeypatch, data_service):
    import config
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', 'sessionid=broken')
    _set_probe(monkeypatch, 'dead', '未检测到登录态')

    resp = api_client.post('/api/douyin-cookie/check')
    assert resp.status == 200
    body = resp.json()
    assert body['status'] == 'dead'      # 手动测活跳过防抖
    assert body['trigger'] == 'manual'

    events = data_service.get_system_events(event_type='cookie_dead')
    assert len(events) == 1


def test_manual_check_alive_returns_healthy(api_client, health_service, monkeypatch):
    import config
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', 'sessionid=good')
    _set_probe(monkeypatch, 'alive', '登录用户 id=123')

    resp = api_client.post('/api/douyin-cookie/check')
    assert resp.status == 200
    body = resp.json()
    assert body['status'] == 'healthy'
    assert body['last_ok_time'] is not None


def test_update_cookie_probes_immediately(api_client, health_service, monkeypatch):
    """更新 Cookie 后响应自带探测结果，粘贴完立刻知道有效与否。"""
    import app as app_module
    monkeypatch.setattr(app_module, 'update_env_value', lambda key, value: None)  # 不污染 .env
    _set_probe(monkeypatch, 'alive', '登录用户 id=123')

    resp = api_client.post('/api/douyin-cookie', data={'cookie': 'sessionid=new', 'reconnect_active': False})
    assert resp.status == 200
    body = resp.json()
    assert body['success'] is True
    assert body['health']['status'] == 'healthy'
    assert body['health']['trigger'] == 'cookie_updated'


def test_clear_cookie_sets_unconfigured(api_client, health_service, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, 'update_env_value', lambda key, value: None)

    resp = api_client.post('/api/douyin-cookie', data={'cookie': '', 'reconnect_active': False})
    assert resp.status == 200
    assert resp.json()['health']['status'] == 'unconfigured'
