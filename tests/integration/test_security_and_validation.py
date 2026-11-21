"""鉴权、同源保护及参数边界的 L1 回归测试。"""
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration


@pytest.fixture
def app_module(monkeypatch):
    import app as module
    assert make_url(module.config.DATABASE_URL).database == "douyin_live_test"
    monkeypatch.setitem(module.app.config, "TESTING", True)
    monkeypatch.setattr(module, "initialize_once_async", lambda: None)
    monkeypatch.setattr(module.config, "AUTH_REQUIRED", True)
    monkeypatch.setattr(module.config, "AUTH_USERNAME", "管理员")
    monkeypatch.setattr(module.config, "AUTH_PASSWORD", "中文密码🔐")
    monkeypatch.setattr(module, "_login_failures", {}, raising=False)
    return module


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


def _login(client, **kwargs):
    return client.post("/login", data={"username": "管理员", "password": "中文密码🔐"}, **kwargs)


@pytest.fixture
def authed_client(client):
    assert _login(client).status_code == 302
    return client


def test_chinese_login_creates_credential_fingerprint_and_secure_cookie(client, app_module):
    response = _login(client)
    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert sess["authenticated"] is True
        assert len(sess["auth_fp"]) == 16
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert "SameSite=Lax" in response.headers["Set-Cookie"]
    for name in ("HTTPONLY", "SAMESITE", "SECURE"):
        assert app_module.app.config[f"SESSION_COOKIE_{name}"] == getattr(app_module.config, f"SESSION_COOKIE_{name}")


def test_password_change_invalidates_session(authed_client, app_module, monkeypatch):
    monkeypatch.setattr(app_module.config, "AUTH_PASSWORD", "新密码")
    assert authed_client.get("/api/proxy").status_code == 401


@pytest.mark.parametrize("testing,fingerprint,expected", [(False, None, 401), (True, None, 200), (True, "wrong", 401), (False, "wrong", 401), (True, "", 401)])
def test_legacy_fixture_compatibility_is_limited_to_testing(client, app_module, testing, fingerprint, expected, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "TESTING", testing)
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        if fingerprint is not None:
            sess["auth_fp"] = fingerprint
    assert client.get("/api/proxy").status_code == expected


def test_unauthenticated_api_is_rejected(client):
    assert client.get("/api/proxy").status_code == 401


def test_eighth_failure_is_rate_limited_and_audited(client, app_module, monkeypatch):
    warning = Mock()
    monkeypatch.setattr(app_module.logger, "warning", warning)
    for _ in range(7):
        assert client.post("/login", data={"username": "bad", "password": "secret-that-must-not-be-logged"}).status_code == 200
    assert client.post("/login", data={"username": "bad", "password": "bad"}).status_code == 429
    assert _login(client).status_code == 429
    assert _login(client, environ_overrides={"REMOTE_ADDR": "127.0.0.2"}, headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 302
    assert warning.call_count >= 8
    assert "127.0.0.1" in str(warning.call_args_list)
    assert "secret-that-must-not-be-logged" not in str(warning.call_args_list)


def test_failed_login_window_expires(client, app_module, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(app_module.time, "monotonic", lambda: now[0])
    for _ in range(8):
        client.post("/login", data={"username": "bad"})
    now[0] += 301
    assert _login(client).status_code == 302


def test_success_resets_consecutive_failures(client):
    for _ in range(7):
        client.post("/login", data={"username": "bad"})
    assert _login(client).status_code == 302
    assert client.post("/login", data={"username": "bad"}).status_code == 200


@pytest.mark.parametrize("next_url", ["//evil.example", "/\\evil.example", "\\\\evil.example", "https://evil.example", "javascript:alert(1)", "///evil.example"])
def test_open_redirects_are_rejected(client, next_url):
    response = client.post("/login", query_string={"next": next_url}, data={"username": "管理员", "password": "中文密码🔐"})
    assert response.headers["Location"] == "/"


def test_local_redirect_is_preserved(client):
    assert client.post("/login", query_string={"next": "/history?room=1"}, data={"username": "管理员", "password": "中文密码🔐"}).headers["Location"] == "/history?room=1"


def test_logout_requires_post_and_clears_session(authed_client):
    assert authed_client.get("/logout").status_code == 405
    assert authed_client.post("/logout").status_code == 302
    assert authed_client.get("/api/proxy").status_code == 401


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"}, {"Referer": "https://evil.example/path"}, {"Origin": "null"}, {"Origin": "http://localhost:9999"}, {"Origin": "https://evil.example", "Referer": "http://localhost/"}])
def test_cross_site_mutations_are_rejected(authed_client, method, headers):
    assert authed_client.open("/logout", method=method, headers=headers).status_code == 403


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://localhost"}, {"Referer": "http://localhost/path"}])
def test_same_host_or_headerless_mutations_work(authed_client, headers):
    assert authed_client.post("/logout", headers=headers).status_code == 302


def test_login_itself_has_origin_protection(client):
    assert _login(client, headers={"Origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize("data", [[], "bad", None, {"enabled": "false"}, {"enabled": 1}, {"type": "ftp"}, {"type": []}, {"port": 0}, {"port": 65536}, {"port": "oops"}, {"port": True}, {"port": 1.5}, {"host": "user@host"}, {"host": "host/path"}, {"host": "bad host"}, {"host": "-bad.example"}, {"host": "999.999.999.999"}, {"host": None}])
def test_invalid_proxy_settings_are_rejected_without_mutation(authed_client, app_module, data):
    original = tuple(getattr(app_module.config, f"PROXY_{name}") for name in ("ENABLED", "TYPE", "HOST", "PORT"))
    assert authed_client.post("/api/proxy", json=data).status_code == 400
    assert tuple(getattr(app_module.config, f"PROXY_{name}") for name in ("ENABLED", "TYPE", "HOST", "PORT")) == original


@pytest.mark.parametrize("host", ["127.0.0.1", "proxy.example", "::1"])
def test_valid_proxy_settings(authed_client, app_module, monkeypatch, host):
    for name in ("ENABLED", "TYPE", "HOST", "PORT"):
        monkeypatch.setattr(app_module.config, f"PROXY_{name}", getattr(app_module.config, f"PROXY_{name}"))
    response = authed_client.post("/api/proxy", json={"enabled": True, "type": "socks5", "host": host, "port": "65535"})
    assert response.status_code == 200
    assert response.json["port"] == 65535


@pytest.mark.parametrize("live_id", ["a@b", "a+b", "a#b", "直播间", "a/b", "a" * 51, 123, []])
def test_invalid_live_id_never_reaches_room_manager(authed_client, app_module, monkeypatch, live_id):
    add = Mock(side_effect=AssertionError("must not create a monitor"))
    monkeypatch.setattr(app_module.room_manager, "add_room", add)
    assert authed_client.post("/api/rooms", json={"live_id": live_id}).status_code == 400
    add.assert_not_called()


@pytest.mark.parametrize("suffix", ["messages?limit=0", "messages?limit=1001", "messages?offset=-1", "messages?offset=bad", "messages?page=0", "messages?page=bad", "contributors?limit=-1", "session-contributors?limit=0", "stats?hours=0", "stats?hours=bad", "stats?hours=2161", "sessions?limit=201", "sessions?limit=bad", "user-search?user_name=a&limit=0", "user-search?user_name=a&session_id=0", "user-messages?user_id=1&session_id=bad", "user-messages?user_id=1&session_id=-1", "user-messages?user_id=1&limit=0"])
def test_room_numeric_parameters_return_400(authed_client, suffix):
    assert authed_client.get(f"/api/rooms/test_room/{suffix}").status_code == 400


@pytest.mark.parametrize("suffix", ["top-likers?limit=bad", "top-likers?limit=1001", "contributors-by-date?source=summary&page_size=0", "contributors-by-date?source=summary&page_size=101", "contributors-by-date?source=summary&page=-1", "sessions/0", "sessions/-1", "sessions/bad", "sessions/1?limit=0", "sessions/1?page=0", "user-search?user_name=a&session_id=bad"])
def test_global_numeric_parameters_return_400(authed_client, suffix):
    assert authed_client.get(f"/api/rooms/{suffix}").status_code == 400


def test_valid_pagination_boundaries_return_data(authed_client):
    response = authed_client.get("/api/rooms/test_room/messages?limit=1&offset=0&page=1")
    assert response.status_code == 200
    assert response.json["pagination"]["page_size"] == 1


@pytest.mark.parametrize("route,service_method", [("", "list_live_rooms"), ("/archived", "get_archived_rooms_summary"), ("/test_room", "get_live_room"), ("/test_room/messages", "get_message_counts"), ("/test_room/contributors", "get_top_contributors"), ("/test_room/session-contributors", "get_current_live_session"), ("/contributors-by-date?source=summary", "get_summary_contributors"), ("/date-range", "get_room_date_range"), ("/test_room/stats", "get_room_stats_history"), ("/stats/summary", "get_stats_summary"), ("/top-likers", "get_top_likers"), ("/test_room/current-session", "get_live_room"), ("/test_room/sessions", "get_live_room"), ("/test_room/sessions/stats", "get_live_room"), ("/sessions/stats", "get_sessions_aggregated_stats"), ("/sessions/1", "get_live_session_stats"), ("/user-messages?user_id=1", "get_user_messages"), ("/user-search?user_name=a", "search_users_by_name")])
def test_database_errors_are_sanitized(authed_client, app_module, monkeypatch, route, service_method):
    private_error = "pymysql SQL SELECT private_password FROM users driver-internal"
    monkeypatch.setattr(app_module.data_service, service_method, Mock(side_effect=RuntimeError(private_error)))
    exception_log = Mock()
    monkeypatch.setattr("api.rooms.logger.exception", exception_log)
    response = authed_client.get("/api/rooms" + route)
    assert response.status_code == 500
    assert private_error not in response.get_data(as_text=True)
    assert response.json["error"]
    exception_log.assert_called_once()


@pytest.mark.parametrize("method,route,target,data", [
    ("POST", "", "room_manager.add_room", {"live_id": "test_room"}),
    ("POST", "/test_room/start", "data_service.get_live_room", None),
    ("POST", "/test_room/stop", "room_manager.stop_room", None),
    ("DELETE", "/test_room", "room_manager.remove_room", None),
    ("POST", "/test_room/restore", "data_service.restore_live_room", None),
    ("DELETE", "/test_room/hard-delete", "room_manager.remove_room", {"confirm_live_id": "test_room"}),
    ("PATCH", "/test_room/config", "data_service.get_live_room", None),
])
def test_mutation_errors_are_sanitized(authed_client, app_module, monkeypatch, method, route, target, data):
    from sqlalchemy.exc import OperationalError

    private_error = "SQL driver password=private-secret"
    service_name, method_name = target.split(".")
    monkeypatch.setattr(getattr(app_module, service_name), method_name,
                        Mock(side_effect=OperationalError("SELECT secret FROM private_table", {}, Exception(private_error))))
    exception_log = Mock()
    monkeypatch.setattr("api.rooms.logger.exception", exception_log)
    response = authed_client.open("/api/rooms" + route, method=method, json=data)
    assert response.status_code == 500
    assert "private" not in response.get_data(as_text=True)
    assert "SQL" not in response.get_data(as_text=True)
    exception_log.assert_called_once()


def test_business_value_error_is_not_exposed_as_validation_error(authed_client, app_module, monkeypatch):
    monkeypatch.setattr(app_module.data_service, "get_top_likers", Mock(side_effect=ValueError("driver secret")))
    response = authed_client.get("/api/rooms/top-likers")
    assert response.status_code == 500
    assert "driver secret" not in response.get_data(as_text=True)


@pytest.mark.parametrize("live_id", ["123456", "a.b-c_d", "a" * 50])
def test_valid_live_ids_are_accepted(authed_client, app_module, monkeypatch, factories, live_id):
    factories.room(live_id=live_id)
    monkeypatch.setattr(app_module.room_manager, "add_room", lambda room_id, *args: room_id)
    assert authed_client.post("/api/rooms", json={"live_id": live_id}).status_code == 200


def test_cookie_update_normalizes_runtime_and_persisted_values(authed_client, app_module, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(app_module.config, "DOUYIN_COOKIE", "original")
    update = Mock(return_value=0)
    monkeypatch.setattr(app_module.room_manager, "update_douyin_cookie", update)
    monkeypatch.setattr(app_module.cookie_health_service, "run_probe", Mock(return_value={"status": "healthy"}))
    response = authed_client.post("/api/douyin-cookie", json={"cookie": "CoOkIe: sessionid=new\nother=value", "reconnect_active": False})
    assert response.status_code == 200
    assert app_module.config.DOUYIN_COOKIE == "sessionid=new other=value"
    update.assert_called_once_with("sessionid=new other=value", reconnect_active=False)
    from dotenv import dotenv_values
    assert dotenv_values(tmp_path / ".env", interpolate=False)["DOUYIN_COOKIE"] == app_module.config.DOUYIN_COOKIE


def test_cookie_persistence_failure_does_not_expose_filesystem_errors(authed_client, app_module, monkeypatch):
    monkeypatch.setattr(app_module.config, "DOUYIN_COOKIE", "original")
    monkeypatch.setattr(app_module, "update_env_value", Mock(side_effect=OSError("private-server-path-secret")))
    monkeypatch.setattr(app_module.room_manager, "update_douyin_cookie", Mock(return_value=0))
    monkeypatch.setattr(app_module.cookie_health_service, "run_probe", Mock(return_value={"status": "healthy"}))
    response = authed_client.post("/api/douyin-cookie", json={"cookie": "sessionid=new"})
    assert response.status_code == 200
    assert response.json["persisted"] is False
    assert "private-server-path-secret" not in response.get_data(as_text=True)


@pytest.mark.parametrize("data", [[], "bad", 123])
def test_socket_join_ignores_non_objects(authed_client, app_module, data):
    socket = app_module.socketio.test_client(app_module.app, flask_test_client=authed_client)
    try:
        assert socket.is_connected()
        socket.emit("join", data)
        assert socket.get_received() == []
    finally:
        socket.disconnect()


def test_socket_join_rechecks_credentials_and_shutdown_is_unregistered(authed_client, app_module, monkeypatch):
    socket = app_module.socketio.test_client(app_module.app, flask_test_client=authed_client)
    try:
        socket.emit("join", {"live_id": "test_room"})
        assert any(event["name"] == "joined" for event in socket.get_received())
        monkeypatch.setattr(app_module.config, "AUTH_PASSWORD", "changed")
        socket.emit("join", {"live_id": "other_room"})
        assert socket.get_received() == []
        assert "shutdown" not in app_module.socketio.server.handlers.get("/", {})
    finally:
        socket.disconnect()


def test_unauthenticated_socket_is_rejected(client, app_module):
    socket = app_module.socketio.test_client(app_module.app, flask_test_client=client)
    assert not socket.is_connected()
