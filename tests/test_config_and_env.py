"""配置及原子写入回归；所有文件写入均隔离到临时目录。"""
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from dotenv import dotenv_values


def _load_config(tmp_path, *, cookie="old-container-cookie", secret="test-secret", testing=False):
    source = Path(__file__).resolve().parents[1] / "config.py"
    script = """
import json, sys
from pathlib import Path
if sys.argv[3] == 'testing':
    sys.modules['pytest'] = object()
namespace = {'__file__': str(Path(sys.argv[2]) / 'config.py'), '__name__': 'isolated_config'}
exec(compile(Path(sys.argv[1]).read_text(), sys.argv[1], 'exec'), namespace)
print(json.dumps({name: namespace.get(name) for name in (
    'DOUYIN_COOKIE', 'SECRET_KEY', 'SESSION_COOKIE_HTTPONLY',
    'SESSION_COOKIE_SAMESITE', 'SESSION_COOKIE_SECURE', 'SOCKETIO_CORS_ALLOWED_ORIGINS')}))
"""
    env = dict(os.environ, DOUYIN_COOKIE=cookie, SECRET_KEY=secret)
    for name in ("SESSION_COOKIE_SAMESITE", "SESSION_COOKIE_SECURE", "SOCKETIO_CORS_ALLOWED_ORIGINS"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", script, str(source), str(tmp_path), "testing" if testing else "production"],
        env=env, cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("old_cookie", ["", "old-container-cookie"])
def test_persisted_cookie_wins_in_production(tmp_path, old_cookie):
    (tmp_path / ".env").write_text("DOUYIN_COOKIE='sessionid=new'\n")
    assert _load_config(tmp_path, cookie=old_cookie)["DOUYIN_COOKIE"] == "sessionid=new"


def test_test_mode_respects_environment(tmp_path):
    (tmp_path / ".env").write_text("DOUYIN_COOKIE='production-cookie'\n")
    assert _load_config(tmp_path, cookie="test-cookie", testing=True)["DOUYIN_COOKIE"] == "test-cookie"


@pytest.mark.parametrize("persisted", ["", "   "])
def test_empty_persisted_cookie_falls_back_to_environment(tmp_path, persisted):
    (tmp_path / ".env").write_text(f"DOUYIN_COOKIE='{persisted}'\n")
    assert _load_config(tmp_path)["DOUYIN_COOKIE"] == "old-container-cookie"


@pytest.mark.parametrize("placeholder", ["", "your-secret-key-change-in-production", "change-this-secret-key-in-production", "your-secret-key"])
def test_secret_is_persistent_and_private(tmp_path, placeholder):
    first = _load_config(tmp_path, secret=placeholder)["SECRET_KEY"]
    key_file = tmp_path / "data" / ".secret_key"
    assert len(first) == 64
    assert int(first, 16) >= 0
    assert key_file.read_text().strip() == first
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    key_file.chmod(0o644)
    assert _load_config(tmp_path, secret=placeholder)["SECRET_KEY"] == first
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_explicit_secret_does_not_create_key_file(tmp_path):
    assert _load_config(tmp_path, secret="my-secure-secret")["SECRET_KEY"] == "my-secure-secret"
    assert not (tmp_path / "data" / ".secret_key").exists()


def test_security_defaults(tmp_path):
    values = _load_config(tmp_path)
    assert values["SESSION_COOKIE_HTTPONLY"] is True
    assert values["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert values["SESSION_COOKIE_SECURE"] is False
    assert values["SOCKETIO_CORS_ALLOWED_ORIGINS"] is None


def test_security_environment_overrides(monkeypatch):
    import config
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "yes")
    assert config._get_bool_env("SESSION_COOKIE_SECURE", False) is True
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "")
    assert config._get_bool_env("SESSION_COOKIE_SECURE", False) is False


@pytest.fixture
def env_writer(monkeypatch, tmp_path):
    import app as app_module
    monkeypatch.setattr(app_module.config, "BASE_DIR", tmp_path)
    return app_module


@pytest.mark.parametrize("prefix", ["Cookie:", "cookie:", "CoOkIe: ", ""])
def test_env_cookie_round_trip(env_writer, tmp_path, prefix, monkeypatch):
    monkeypatch.setenv("COOKIE_FRAGMENT", "must-not-expand")
    cookie = "sessionid=a$b; quoted='one'\"two\"; slash=\\path; literal=${COOKIE_FRAGMENT}"
    env_writer.update_env_value("DOUYIN_COOKIE", prefix + cookie)
    env_path = tmp_path / ".env"
    assert dotenv_values(env_path, interpolate=False)["DOUYIN_COOKIE"] == cookie
    assert _load_config(tmp_path)["DOUYIN_COOKIE"] == cookie
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_env_preserves_other_settings_and_replaces_duplicates(env_writer, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("# retained\nexport DOUYIN_COOKIE=old\nDOUYIN_COOKIE=duplicate\nOTHER=untouched")
    env_writer.update_env_value("DOUYIN_COOKIE", "sessionid=new")
    assert dotenv_values(env_path, interpolate=False) == {"DOUYIN_COOKIE": "sessionid=new", "OTHER": "untouched"}
    assert "# retained" in env_path.read_text()
    assert "duplicate" not in env_path.read_text()


def test_env_write_failure_keeps_original_and_removes_temporary_file(env_writer, tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    original = "OTHER=original\n"
    env_path.write_text(original)

    def fail_replace(source, destination):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(env_writer.os, "replace", fail_replace)
    with pytest.raises(OSError):
        env_writer.update_env_value("DOUYIN_COOKIE", "new")
    assert env_path.read_text() == original
    assert list(tmp_path.iterdir()) == [env_path]


def test_concurrent_env_writes_do_not_lose_settings(env_writer, tmp_path):
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda number: env_writer.update_env_value(f"SETTING_{number}", str(number)), range(12)))
    assert dotenv_values(tmp_path / ".env", interpolate=False) == {f"SETTING_{n}": str(n) for n in range(12)}


def test_scheduler_registration_is_idempotent(monkeypatch):
    import app as app_module
    from apscheduler.schedulers.background import BackgroundScheduler
    from services.scheduler_service import SchedulerService

    scheduler = BackgroundScheduler()
    service = SchedulerService(app_module.room_manager, app_module.data_service)
    service.scheduler = scheduler
    monkeypatch.setattr(app_module, "scheduler_service", service)
    app_module.register_scheduler_jobs()
    app_module.register_scheduler_jobs()
    jobs = scheduler.get_jobs()
    assert [job.id for job in jobs] == ["cookie_health_tick"]
    assert jobs[0].next_run_time is not None


def test_signal_shutdown_cleans_up_once(monkeypatch):
    import signal
    from types import SimpleNamespace
    from unittest.mock import Mock
    import app as app_module

    monkeypatch.setattr(app_module, "_shutdown_complete", False)
    callbacks = [Mock() for _ in range(4)]
    monkeypatch.setattr(app_module, "stop_status_display", callbacks[0])
    monkeypatch.setattr(app_module.room_manager, "shutdown", callbacks[1])
    monkeypatch.setattr(app_module.scheduler_service, "stop", callbacks[2])
    monkeypatch.setattr(app_module.scheduler_service, "scheduler", SimpleNamespace(running=True))
    monkeypatch.setattr(app_module.data_service, "close_session", callbacks[3])
    for _ in range(2):
        with pytest.raises(SystemExit) as exit_info:
            app_module.handle_termination_signal(signal.SIGTERM, None)
        assert exit_info.value.code == 0
    assert all(callback.call_count == 1 for callback in callbacks)


def test_wsgi_initialization_registers_jobs_and_starts_once(monkeypatch):
    from unittest.mock import Mock
    import app as app_module
    from services.scheduler_service import SchedulerService

    service = SchedulerService(app_module.room_manager, app_module.data_service)
    start = Mock()
    monkeypatch.setattr(service, "start", start)
    monkeypatch.setattr(app_module, "scheduler_service", service)
    monkeypatch.setitem(app_module.app.config, "TESTING", False)
    monkeypatch.setattr(app_module.app, "_initialized", False, raising=False)
    app_module.initialize_once_async()
    app_module.initialize_once_async()
    start.assert_called_once()
    assert service.scheduler.get_job("cookie_health_tick") is not None


def test_main_startup_masks_database_password_and_registers_signals(monkeypatch):
    import runpy
    import signal
    from unittest.mock import Mock
    import app as app_module
    from flask_socketio import SocketIO
    from services.scheduler_service import SchedulerService
    from sqlalchemy.engine import make_url

    logs = Mock()
    register_signal = Mock()
    run_server = Mock()
    monkeypatch.setattr("utils.logger.get_logger", lambda name: logs)
    monkeypatch.setattr(signal, "signal", register_signal)
    monkeypatch.setattr(SocketIO, "run", run_server)
    monkeypatch.setattr(SchedulerService, "start", lambda self: None)
    monkeypatch.setattr(app_module.config, "STATUS_DISPLAY_ENABLED", False)
    runpy.run_path(app_module.__file__, run_name="__main__")
    run_server.assert_called_once()
    assert [call.args[0] for call in register_signal.call_args_list] == [signal.SIGTERM, signal.SIGINT]
    log_text = str(logs.info.call_args_list)
    assert make_url(app_module.config.DATABASE_URL).password not in log_text
    assert make_url(app_module.config.DATABASE_URL).render_as_string(hide_password=True) in log_text
