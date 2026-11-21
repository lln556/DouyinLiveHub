"""Maintenance only operates on temporary databases and mocked dump executables."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
from datetime import timedelta

from sqlalchemy import event, inspect, text

from models.database import ChatMessage, GiftMessage, LiveSession, RoomStats, UserContribution, get_china_now
from services.data_service import DataService


ROOT = Path(__file__).resolve().parents[1]


def repair_module():
    spec = importlib.util.spec_from_file_location('repair_historical_data', ROOT / 'scripts/repair_historical_data.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_creates_missing_indexes_idempotently(tmp_path):
    from app import migrate_database
    svc = DataService(f'sqlite:///{tmp_path / "migration.db"}')
    svc.create_tables()
    with svc.engine.begin() as conn:
        conn.execute(text('DROP INDEX idx_chat_session_time'))
        conn.execute(text('DROP INDEX idx_contribution_room_likes'))
    migrate_database(svc.engine)
    migrate_database(svc.engine)
    assert 'idx_chat_session_time' in {i['name'] for i in inspect(svc.engine).get_indexes('chat_messages')}
    assert 'idx_contribution_room_likes' in {i['name'] for i in inspect(svc.engine).get_indexes('user_contributions')}
    svc.close_session()


def test_backup_job_registration_is_deferred_and_respects_switch(monkeypatch):
    import app as module
    monkeypatch.setenv('AUTO_DB_BACKUP_ENABLED', 'true')
    module.scheduler_service.register_configured_backup()
    job = module.scheduler_service.scheduler.get_job('daily_database_backup')
    assert job is not None
    assert str(job.trigger) == "cron[hour='4', minute='30']"
    module.scheduler_service.register_configured_backup()
    assert sum(j.id == 'daily_database_backup' for j in module.scheduler_service.get_jobs()) == 1
    monkeypatch.setenv('AUTO_DB_BACKUP_ENABLED', 'false')
    module.scheduler_service.remove_job('daily_database_backup')
    module.scheduler_service.register_configured_backup()
    assert module.scheduler_service.scheduler.get_job('daily_database_backup') is None


def test_historical_repair_dry_run_orphans_peak_and_retention(tmp_path):
    module = repair_module()
    svc = DataService(f'sqlite:///{tmp_path / "repair.db"}')
    svc.create_tables()
    now = get_china_now().replace(tzinfo=None)
    for rid in ['complete', 'expired']:
        svc.create_live_room(rid, anchor_name='private-anchor')
    db = svc.get_session()
    with db:
        first = LiveSession(live_id='complete', start_time=now - timedelta(hours=3), end_time=now - timedelta(hours=1), status='ended', peak_viewer_count=10)
        overlap = LiveSession(live_id='complete', start_time=now - timedelta(hours=2), end_time=now - timedelta(minutes=30), status='ended', peak_viewer_count=20)
        historic = LiveSession(live_id='expired', start_time=now - timedelta(days=100), status='ended', total_income=999)
        recent = LiveSession(live_id='expired', start_time=now - timedelta(hours=2), status='ended')
        db.add_all([first, overlap, historic, recent]); db.flush()
        affected = overlap.id
        db.add(RoomStats(live_id='complete', stats_at=now - timedelta(minutes=45), current_user_count=300))
        db.add(ChatMessage(live_id='complete', user_id='u', user_name='private-user', content='hi', created_at=now - timedelta(minutes=45)))
        db.add(GiftMessage(live_id='complete', user_id='u', user_name='private-user', gift_name='gift', gift_count=3, gift_price=.25, total_value=.75, created_at=now - timedelta(minutes=30) + timedelta(seconds=4)))
        db.add(GiftMessage(live_id='expired', live_session_id=recent.id, user_id='u', user_name='private-user', gift_name='gift', gift_count=1, gift_price=1, total_value=1))
        db.add(UserContribution(live_id='complete', user_id='u', user_name='private-user', total_score=.751))
        db.add(UserContribution(live_id='expired', user_id='u', user_name='private-user', total_score=999))
        db.commit()
    summary = module.repair_data(svc, apply=False)
    assert summary['orphan_chats'] == summary['orphan_gifts'] == summary['contributions'] == 1
    with svc.get_session() as db:
        assert db.query(ChatMessage).one().live_session_id is None
        assert db.get(LiveSession, affected).peak_viewer_count == 20
    summary2 = module.repair_data(svc, apply=True)
    assert summary == summary2
    with svc.get_session() as db:
        row = db.get(LiveSession, affected)
        assert row.peak_viewer_count == 300
        assert (row.total_income, row.total_gift_count, row.total_chat_count) == (.75, 3, 1)
        assert db.query(ChatMessage).one().live_session_id == affected
        assert db.query(UserContribution).filter_by(live_id='complete').one().total_score == .75
        assert db.query(UserContribution).filter_by(live_id='expired').one().total_score == 999
    assert not any(module.repair_data(svc, apply=False).values())
    svc.close_session()


def test_backup_native_and_docker_fallback_secure_files(tmp_path):
    project = tmp_path / 'project'
    (project / 'scripts').mkdir(parents=True)
    shutil.copy(ROOT / 'scripts/backup_db.sh', project / 'scripts/backup_db.sh')
    (project / '.env').write_text('MYSQL_HOST=mockdb\nMYSQL_DATABASE=isolated\nMYSQL_USER=test\nMYSQL_PASSWORD=secret\n')
    bindir = tmp_path / 'bin'; bindir.mkdir()
    native = bindir / 'mysqldump'
    native.write_text('#!/bin/bash\nif [[ "$1" == "--help" ]]; then echo no-tablespaces; exit 0; fi\nif [[ "${MOCK_FAIL:-}" == "1" ]]; then exit 1; fi\necho "-- mocked SQL"\n')
    native.chmod(0o755)
    docker = bindir / 'docker'
    docker.write_text('#!/bin/bash\necho "-- mocked fallback SQL"\n'); docker.chmod(0o755)
    backups = project / 'data/backups'; backups.mkdir(parents=True)
    for name in ['douyin_live_20000101_000000.sql.gz', 'pre_hardening_old.sql.gz', 'env.old']:
        path = backups / name; path.write_text('old'); os.utime(path, (1, 1))
    env = {k: v for k, v in os.environ.items() if not k.startswith('MYSQL_') and k != 'DATABASE_URL'}
    env.update(PATH=str(bindir) + ':' + os.environ['PATH'], PYTHON_BIN=os.environ.get('UV_PROJECT_ENVIRONMENT', '/usr') + '/bin/python')
    for fail in ['0', '1']:
        env['MOCK_FAIL'] = fail
        result = subprocess.run(['bash', str(project / 'scripts/backup_db.sh')], env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert 'secret' not in result.stdout + result.stderr
    exports = list(backups.glob('douyin_live_*.sql.gz'))
    assert exports
    assert all(p.stat().st_mode & 0o077 == 0 for p in exports)
    assert not (backups / 'douyin_live_20000101_000000.sql.gz').exists()
    assert (backups / 'pre_hardening_old.sql.gz').exists() and (backups / 'env.old').exists()
    for path in exports:
        assert subprocess.run(['gzip', '-t', str(path)]).returncode == 0



def test_repair_preview_emits_no_sql_writes_or_private_details(tmp_path, capsys, monkeypatch):
    module = repair_module()
    svc = DataService(f'sqlite:///{tmp_path / "privacy.db"}')
    svc.create_tables()
    svc.create_live_room('private-id', anchor_name='private-anchor')
    live = svc.create_live_session('private-id', peak_viewer_count=1)
    svc.save_room_stats('private-id', current_user_count=99)
    sql = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)
    event.listen(svc.engine, 'before_cursor_execute', capture)
    monkeypatch.setattr(module, 'DataService', lambda: svc)
    assert module.main([]) == 0
    output = capsys.readouterr()
    assert 'mode=dry-run' in output.out and 'peaks=1' in output.out
    assert 'private-' not in output.out + output.err
    assert all(not q.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) for q in sql)


def test_backup_failure_leaves_no_published_archive(tmp_path):
    project = tmp_path / 'project'
    (project / 'scripts').mkdir(parents=True)
    shutil.copy(ROOT / 'scripts/backup_db.sh', project / 'scripts/backup_db.sh')
    bindir = tmp_path / 'bin'; bindir.mkdir()
    for name in ['mysqldump', 'docker']:
        path = bindir / name
        path.write_text('#!/bin/bash\nexit 1\n'); path.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + ':' + os.environ['PATH'], DATABASE_URL='mysql+pymysql://test:secret@mock/isolated')
    result = subprocess.run(['bash', str(project / 'scripts/backup_db.sh')], env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert not list((project / 'data/backups').iterdir())
    assert 'secret' not in result.stdout + result.stderr


def test_repair_apply_does_not_overwrite_newer_peak_or_counters(tmp_path, monkeypatch):
    from sqlalchemy import update
    module = repair_module()
    svc = DataService(f'sqlite:///{tmp_path / "optimistic.db"}')
    svc.create_tables()
    now = get_china_now().replace(tzinfo=None)
    svc.create_live_room('race', anchor_name='test')
    live = svc.create_live_session('race', start_time=now - timedelta(hours=1), peak_viewer_count=1, status='ended')
    svc.save_room_stats('race', stats_at=now, current_user_count=100)
    svc.save_chat_message('race', user_id='u', user_name='test', content='hi')
    svc.save_gift_message('race', live_session_id=live.id, user_id='u', user_name='test', gift_name='gift', gift_count=1, gift_price=1, total_value=1)
    contribution = svc.update_user_contribution('race', 'test', 'u', 'test', gift_value=1.001)
    # An implementation using explicit atomic statements has no ORM dirty flush;
    # inject the change at its first maintenance UPDATE instead.
    fired = False
    def capture(conn, cursor, statement, parameters, context, executemany):
        nonlocal fired
        if not fired and statement.lstrip().upper().startswith('UPDATE'):
            fired = True
            conn.execute(update(LiveSession).where(LiveSession.id == live.id).values(peak_viewer_count=999, total_income=10, total_chat_count=10))
            conn.execute(update(UserContribution).where(UserContribution.id == contribution.id).values(total_score=10))
    event.listen(svc.engine, 'before_cursor_execute', capture)
    try:
        module.repair_data(svc, apply=True)
    finally:
        event.remove(svc.engine, 'before_cursor_execute', capture)
    with svc.get_session() as db:
        row = db.get(LiveSession, live.id)
        assert row.peak_viewer_count == 999
        assert row.total_income == row.total_chat_count == 10
        assert db.get(UserContribution, contribution.id).total_score == 10
    svc.close_session()


def test_mariadb_tls_failure_retries_supported_ssl_flag(tmp_path):
    project = tmp_path / 'project'; (project / 'scripts').mkdir(parents=True)
    shutil.copy(ROOT / 'scripts/backup_db.sh', project / 'scripts/backup_db.sh')
    bindir = tmp_path / 'bin'; bindir.mkdir()
    dump = bindir / 'mysqldump'
    dump.write_text('#!/bin/bash\nif [[ "$1" == "--help" ]]; then echo "ssl ssl-verify-server-cert no-tablespaces"; exit 0; fi\nif [[ "$*" == *ssl-mode* ]]; then exit 2; fi\nif [[ "$*" != *skip-ssl* ]]; then echo "TLS/SSL error: certificate verify failed" >&2; exit 1; fi\necho "-- mock MariaDB dump"\n')
    dump.chmod(0o755)
    docker = bindir / 'docker'; docker.write_text('#!/bin/bash\nexit 1\n'); docker.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + ':' + os.environ['PATH'], DATABASE_URL='mysql+pymysql://test:secret@mock/isolated')
    result = subprocess.run(['bash', str(project / 'scripts/backup_db.sh')], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert len(list((project / 'data/backups').glob('douyin_live_*.sql.gz'))) == 1


def test_repair_does_not_recount_a_live_gift_before_handler_increment(tmp_path):
    module = repair_module()
    svc = DataService(f'sqlite:///{tmp_path / "inflight.db"}')
    svc.create_tables()
    svc.create_live_room('live', anchor_name='test')
    live = svc.create_live_session('live', start_time=get_china_now() - timedelta(hours=1))
    svc.save_gift_message('live', live_session_id=live.id, user_id='u', user_name='test', gift_name='gift', gift_count=1, gift_price=1, total_value=1)
    svc.update_user_contribution('live', 'test', 'u', 'test', gift_value=.001)
    svc.save_chat_message('live', user_id='u', user_name='test', content='orphan')
    summary = module.repair_data(svc, apply=True)
    assert summary['orphan_chats'] == 1
    assert summary['sessions'] == summary['contributions'] == 0
    svc.increment_session_stats(live.id, income_delta=1, gift_count_delta=1)
    svc.update_user_contribution('live', 'test', 'u', 'test', gift_value=1, gift_count=1)
    assert svc.get_current_live_session('live').total_income == 1
    assert abs(svc.get_user_contribution('live', 'u').total_score - 1.001) < 1e-8
    svc.close_session()
