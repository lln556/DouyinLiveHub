"""probe_douyin_cookie 纯函数单测：alive / dead / inconclusive 三种判定。"""
from unittest.mock import patch

import requests

from services.cookie_health import CookieProbeResult, probe_douyin_cookie


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def _probe_with(response=None, exc=None):
    with patch("services.cookie_health.requests.get") as mock_get:
        if exc is not None:
            mock_get.side_effect = exc
        else:
            mock_get.return_value = response
        return probe_douyin_cookie("sessionid=abc; ttwid=xyz")


def test_logged_in_user_returns_alive():
    resp = _FakeResponse(payload={"status_code": 0, "data": {"id_str": "12345"}})
    result = _probe_with(response=resp)
    assert result.outcome == "alive"


def test_anonymous_user_returns_dead():
    """接口正常返回但用户 id 为 0/空 → 明确未登录。"""
    resp = _FakeResponse(payload={"status_code": 0, "data": {"id_str": "0"}})
    assert _probe_with(response=resp).outcome == "dead"

    resp = _FakeResponse(payload={"status_code": 0, "data": {}})
    assert _probe_with(response=resp).outcome == "dead"


def test_error_status_code_returns_dead():
    resp = _FakeResponse(payload={"status_code": 8, "data": None})
    assert _probe_with(response=resp).outcome == "dead"


def test_network_error_returns_inconclusive():
    result = _probe_with(exc=requests.exceptions.ConnectionError("boom"))
    assert result.outcome == "inconclusive"


def test_http_5xx_returns_inconclusive():
    resp = _FakeResponse(status_code=502)
    assert _probe_with(response=resp).outcome == "inconclusive"


def test_non_json_returns_inconclusive():
    resp = _FakeResponse(raise_json=True)
    assert _probe_with(response=resp).outcome == "inconclusive"
