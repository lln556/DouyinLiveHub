"""
抖音直播监控平台 - Flask应用入口
支持多直播间24小时监控、数据持久化存储
"""
import os
import gzip
import hashlib
import ipaddress
import re
import secrets
import signal
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_socketio import SocketIO, emit, join_room
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.engine import make_url
from werkzeug.wsgi import FileWrapper

import config
from models.database import Base, get_china_now
from services.data_service import DataService
from services.room_manager import RoomManager, MonitoredRoom
from services.scheduler_service import SchedulerService
from services.cookie_health import CookieHealthService, COOKIE_HEALTH_TICK_INTERVAL
from api.rooms import init_rooms_api
from utils.logger import get_logger
from utils.status_display import StatusDisplay

# 使用 loguru 全局日志
logger = get_logger("app")
initialization_lock = threading.Lock()
_env_write_lock = threading.Lock()
_login_failure_lock = threading.Lock()
_login_failures = {}
_scheduler_jobs_lock = threading.Lock()
_asset_version_lock = threading.Lock()
_asset_version_cache = {}
_gzip_cache_lock = threading.Lock()
_gzip_cache = {}
_shutdown_lock = threading.Lock()
_shutdown_complete = False

_PUBLIC_STATIC_ENDPOINTS = {'static', 'favicon', 'serve_level_img', 'serve_fansclub_img', 'serve_emoji_img'}


class _PublicStaticSessionInterface(SecureCookieSessionInterface):
    def save_session(self, app, session, response):
        # Flask saves sessions after after_request; permanent cookie refresh
        # would otherwise add Set-Cookie and Vary: Cookie to public assets.
        if request.endpoint in _PUBLIC_STATIC_ENDPOINTS:
            return
        super().save_session(app, session, response)


# 创建Flask应用
app = Flask(__name__)
app.session_interface = _PublicStaticSessionInterface()
app.config['SECRET_KEY'] = config.SECRET_KEY
app.config['SESSION_COOKIE_HTTPONLY'] = config.SESSION_COOKIE_HTTPONLY
app.config['SESSION_COOKIE_SAMESITE'] = config.SESSION_COOKIE_SAMESITE
app.config['SESSION_COOKIE_SECURE'] = config.SESSION_COOKIE_SECURE
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=config.AUTH_SESSION_DAYS)


def asset_v(rel_path: str) -> str:
    """Return a cached content fingerprint for files strictly inside static."""
    try:
        static_root = Path(app.static_folder).resolve()
        resolved_path = (static_root / rel_path).resolve()
        if resolved_path == static_root or not resolved_path.is_relative_to(static_root):
            return '1'
        stat = resolved_path.stat()
        key = (resolved_path, stat.st_mtime_ns, stat.st_size)
        with _asset_version_lock:
            if key not in _asset_version_cache:
                version = hashlib.md5(resolved_path.read_bytes()).hexdigest()[:8]
                if len(_asset_version_cache) >= 128:
                    _asset_version_cache.pop(next(iter(_asset_version_cache)))
                _asset_version_cache[key] = version
            return _asset_version_cache[key]
    except (OSError, ValueError):
        return '1'


@app.context_processor
def inject_asset_version():
    return {'asset_v': asset_v}


def _accepts_gzip(header_value: str) -> bool:
    """An explicit gzip quality takes precedence over the wildcard."""
    qualities = {}
    for item in header_value.lower().split(','):
        encoding, *parameters = item.split(';')
        encoding = encoding.strip()
        if encoding not in {'gzip', '*'}:
            continue
        quality = 1.0
        for parameter in parameters:
            name, _, value = parameter.partition('=')
            if name.strip() == 'q':
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
                if not 0 <= quality <= 1:
                    quality = 0.0
        qualities[encoding] = quality
    return qualities.get('gzip', qualities.get('*', 0.0)) > 0


@app.after_request
def optimize_response_headers_and_compress(response):
    if request.endpoint in _PUBLIC_STATIC_ENDPOINTS and response.status_code in (200, 304):
        response.vary.discard('Cookie')
        response.vary.discard('cookie')
        if 'v' in request.args or request.path.startswith(('/static/vendor/', '/level_img/', '/fansclub_img/', '/emoji_img/')):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        else:
            response.headers['Cache-Control'] = 'public, max-age=86400'

    mimetype = response.mimetype or ''
    compressible = (mimetype in {'application/json', 'application/javascript', 'text/javascript', 'image/svg+xml'}
                    or mimetype.startswith('text/'))
    if (request.method in ('GET', 'HEAD') and compressible
            and (200 <= response.status_code < 300 or response.status_code == 304)):
        response.vary.add('Accept-Encoding')

    if (request.method != 'GET' or response.status_code != 200 or not compressible
            or 'Content-Encoding' in response.headers
            or not _accepts_gzip(request.headers.get('Accept-Encoding', ''))):
        return response

    if response.direct_passthrough:
        length = response.content_length
        if length is None or not 512 <= length <= 10 * 1024 * 1024:
            return response
        # send_file uses an iterable FileWrapper. Buffer only bounded files;
        # arbitrary streaming generators must remain lazy.
        if response.is_streamed and not isinstance(response.response, FileWrapper):
            return response
        response.direct_passthrough = False
        response.make_sequence()
    if response.is_streamed:
        return response

    raw = response.get_data()
    if len(raw) < 512:
        return response

    if request.endpoint in {'static', 'favicon'}:
        key = (request.path, response.headers.get('ETag', ''), len(raw))
        with _gzip_cache_lock:
            if key not in _gzip_cache:
                compressed = gzip.compress(raw, compresslevel=6)
                if len(_gzip_cache) >= 64:
                    _gzip_cache.pop(next(iter(_gzip_cache)))
                _gzip_cache[key] = compressed
            compressed = _gzip_cache[key]
    else:
        compressed = gzip.compress(raw, compresslevel=6)

    if len(compressed) < len(raw):
        response.set_data(compressed)
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = str(len(compressed))
        etag = response.headers.get('ETag')
        if etag and not etag.startswith('W/'):
            response.headers['ETag'] = f'W/{etag}'
    return response


# 创建Socket.IO实例
socketio = SocketIO(
    app,
    cors_allowed_origins=config.SOCKETIO_CORS_ALLOWED_ORIGINS,
    async_mode=config.SOCKETIO_ASYNC_MODE,
    logger=config.SOCKETIO_LOGGER,
    engineio_logger=config.SOCKETIO_ENGINEIO_LOGGER
)

# 初始化服务
data_service = DataService(config.DATABASE_URL)
# 创建数据库表
data_service.create_tables()

# 数据库迁移：添加新列
def migrate_database(engine):
    """检查并添加新增的数据库列"""
    inspector = sa_inspect(engine)
    migrations = [
        ('chat_messages', 'fans_club_level', 'INTEGER DEFAULT 0'),
        ('gift_messages', 'fans_club_level', 'INTEGER DEFAULT 0'),
        ('room_stats', 'total_like_count', 'INTEGER DEFAULT 0'),
        ('user_contributions', 'like_count', 'INTEGER DEFAULT 0'),
        ('user_contributions', 'gender', 'INTEGER'),
        ('user_contributions', 'follower_count', 'INTEGER'),
        ('user_contributions', 'following_count', 'INTEGER'),
        ('user_contributions', 'age_range', 'INTEGER'),
        ('user_contributions', 'fans_club_level', 'INTEGER DEFAULT 0'),
        ('user_contributions', 'user_level', 'INTEGER DEFAULT 0'),
        ('live_sessions', 'total_like_count', 'INTEGER DEFAULT 0'),
        ('live_rooms', 'archived_at', 'DATETIME NULL'),
        ('user_contributions', 'display_id', 'VARCHAR(100) NULL'),
        ('chat_messages', 'emoji_list', 'JSON NULL'),
    ]
    with engine.connect() as conn:
        for table, column, col_type in migrations:
            columns = [c['name'] for c in inspector.get_columns(table)]
            if column not in columns:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {col_type}'))
                logger.info(f"数据库迁移: {table} 添加列 {column} ({col_type})")
        conn.commit()
    # create_all does not add indexes to tables that already exist.
    for table_name, table in Base.metadata.tables.items():
        existing = {item['name'] for item in sa_inspect(engine).get_indexes(table_name)}
        for index in table.indexes:
            if index.name in existing:
                continue
            try:
                index.create(bind=engine, checkfirst=True)
            except Exception:
                # A concurrent startup may have created it after inspection.
                if index.name not in {item['name'] for item in sa_inspect(engine).get_indexes(table_name)}:
                    raise

migrate_database(data_service.engine)

# 初始化房间管理器
room_manager = RoomManager(data_service, socketio)

# 初始化调度服务
scheduler_service = SchedulerService(room_manager, data_service)

# 初始化 Cookie 健康检测服务
cookie_health_service = CookieHealthService(room_manager, data_service)

# 初始化终端状态面板
status_display = StatusDisplay(room_manager) if config.STATUS_DISPLAY_ENABLED else None


def start_status_display():
    """按配置启动终端状态面板。"""
    if status_display:
        status_display.start()
    else:
        logger.info("终端状态面板已禁用")


def stop_status_display():
    """按配置停止终端状态面板。"""
    if status_display:
        status_display.stop()


def update_env_value(key: str, value: str):
    """更新项目 .env 中的单个键值；不存在则追加。"""
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
        raise ValueError('环境变量名称无效')
    if key == 'DOUYIN_COOKIE':
        value = _normalize_cookie(value)
    env_path = config.BASE_DIR / '.env'
    escaped_value = value.replace('\\', '\\\\').replace("'", "\\'")
    line = f"{key}='{escaped_value}'\n"
    key_pattern = re.compile(rf'^\s*(?:export\s+)?{re.escape(key)}\s*=')
    with _env_write_lock:
        lines = env_path.read_text(encoding='utf-8').splitlines(keepends=True) if env_path.exists() else []
        updated_lines = []
        updated = False
        for current in lines:
            if key_pattern.match(current):
                if not updated:
                    updated_lines.append(line)
                    updated = True
            else:
                updated_lines.append(current)
        if not updated:
            if updated_lines and not updated_lines[-1].endswith('\n'):
                updated_lines[-1] += '\n'
            updated_lines.append(line)

        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=env_path.parent,
                                             prefix='.env.', delete=False) as temporary:
                temporary_path = temporary.name
                os.chmod(temporary_path, 0o600)
                temporary.write(''.join(updated_lines))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, env_path)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)


def _normalize_cookie(value):
    """统一浏览器复制的 Cookie 头和多行格式。"""
    return re.sub(r'^cookie:\s*', '', ' '.join(value.splitlines()).strip(), flags=re.IGNORECASE)


# ==================== 路由定义 ====================

def _credentials_fingerprint():
    return hashlib.sha256(f'{config.AUTH_USERNAME}\0{config.AUTH_PASSWORD}'.encode('utf-8')).hexdigest()[:16]


def _secure_equals(left, right):
    return secrets.compare_digest(
        hashlib.sha256(left.encode('utf-8')).digest(),
        hashlib.sha256(right.encode('utf-8')).digest(),
    )


def is_authenticated():
    """检查当前请求是否已登录。"""
    if not config.AUTH_REQUIRED:
        return True
    if session.get('authenticated') is not True:
        return False
    if app.config.get('TESTING') and 'auth_fp' not in session:
        return True
    fingerprint = session.get('auth_fp')
    return isinstance(fingerprint, str) and _secure_equals(fingerprint, _credentials_fingerprint())


def get_safe_next(default='/'):
    """只允许站内跳转，避免登录后开放重定向。"""
    next_url = request.args.get('next') or request.form.get('next') or default
    next_url = next_url.replace('\\', '/')
    parsed = urlparse(next_url)
    if next_url.startswith('//') or parsed.netloc or parsed.scheme or not next_url.startswith('/'):
        return default
    return next_url


@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录页。账号密码来自环境变量 AUTH_USERNAME/AUTH_PASSWORD。"""
    if not config.AUTH_REQUIRED:
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        client_ip = request.remote_addr or 'unknown'
        now = time.monotonic()
        with _login_failure_lock:
            # 清理不再处于滑动窗口内的 IP，避免长期积累。
            for old_ip in list(_login_failures):
                if not _login_failures[old_ip] or _login_failures[old_ip][-1] <= now - 300:
                    del _login_failures[old_ip]
            failures = _login_failures.setdefault(client_ip, deque())
            while failures and failures[0] <= now - 300:
                failures.popleft()
            if len(failures) >= 8:
                logger.warning(f'登录限流: ip={client_ip}, failures={len(failures)}')
                return render_template('login.html', error='登录失败次数过多，请稍后重试', next_url=get_safe_next()), 429

            username_ok = _secure_equals(username, config.AUTH_USERNAME)
            password_ok = _secure_equals(password, config.AUTH_PASSWORD)
            if username_ok and password_ok:
                _login_failures.pop(client_ip, None)
                session.clear()
                session.permanent = True
                session['authenticated'] = True
                session['username'] = username
                session['auth_fp'] = _credentials_fingerprint()
                return redirect(get_safe_next())
            failures.append(now)
            logger.warning(f'登录失败: ip={client_ip}, failures={len(failures)}')
            if len(failures) >= 8:
                return render_template('login.html', error='登录失败次数过多，请稍后重试', next_url=get_safe_next()), 429
        error = '账号或密码错误'

    return render_template('login.html', error=error, next_url=get_safe_next())


@app.route('/logout', methods=['POST'])
def logout():
    """退出登录。"""
    session.clear()
    return redirect(url_for('login'))


@app.route('/favicon.ico')
def favicon():
    """浏览器标签页图标。"""
    return send_from_directory(app.static_folder, 'favicon.svg', mimetype='image/svg+xml')


@app.route('/healthz')
def healthz():
    """容器健康检查端点，不触发监控自动启动。"""
    return jsonify({'status': 'ok'})


# 静态文件路由
@app.route('/level_img/<path:filename>')
def serve_level_img(filename):
    """提供等级图标静态文件"""
    return send_from_directory('data/level_img', filename)


@app.route('/fansclub_img/<path:filename>')
def serve_fansclub_img(filename):
    """提供粉丝团等级图标静态文件"""
    return send_from_directory('data/fansclub_img', filename)


@app.route('/emoji_img/<path:filename>')
def serve_emoji_img(filename):
    """提供抖音官方表情静态文件"""
    return send_from_directory('data/emoji_img', filename)


@app.route('/')
def index():
    """首页 - 房间列表"""
    return render_template('index.html')


@app.route('/room/<live_id>')
def room_detail(live_id):
    """房间详情页"""
    room = data_service.get_live_room(live_id)
    if not room:
        return "房间不存在", 404
    return render_template('room.html', live_id=live_id)


@app.route('/stats')
def stats_page():
    """数据统计页"""
    return render_template('stats.html')


@app.route('/history')
def history_page():
    """历史数据页"""
    return render_template('history.html')


@app.route('/api/proxy', methods=['GET'])
def get_proxy_config():
    """获取代理配置"""
    return jsonify({
        'enabled': config.PROXY_ENABLED,
        'host': config.PROXY_HOST,
        'port': config.PROXY_PORT,
        'type': config.PROXY_TYPE
    })


@app.route('/api/proxy', methods=['POST'])
def update_proxy_config():
    """更新代理配置（仅限运行时，重启后恢复为配置文件值）"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '代理配置必须为对象'}), 400
    enabled = data.get('enabled', False)
    proxy_type = data.get('type', config.PROXY_TYPE)
    host = data.get('host', config.PROXY_HOST)
    raw_port = data.get('port', config.PROXY_PORT)
    if not isinstance(enabled, bool):
        return jsonify({'error': 'enabled 必须为布尔值'}), 400
    if proxy_type not in ('http', 'socks5'):
        return jsonify({'error': '代理类型必须为 http 或 socks5'}), 400
    if isinstance(raw_port, bool) or not isinstance(raw_port, (int, str)):
        return jsonify({'error': '代理端口必须为 1-65535 的整数'}), 400
    try:
        port = int(raw_port)
    except (ValueError, TypeError):
        return jsonify({'error': '代理端口必须为 1-65535 的整数'}), 400
    if not 1 <= port <= 65535:
        return jsonify({'error': '代理端口必须为 1-65535 的整数'}), 400
    if not _is_valid_proxy_host(host):
        return jsonify({'error': '代理主机必须为合法主机名或 IP 地址'}), 400

    # 更新运行时配置
    config.PROXY_ENABLED = enabled
    config.PROXY_HOST = host
    config.PROXY_PORT = port
    config.PROXY_TYPE = proxy_type

    logger.info(f"代理配置已更新: enabled={enabled}, host={config.PROXY_HOST}, port={config.PROXY_PORT}")

    return jsonify({
        'success': True,
        'enabled': config.PROXY_ENABLED,
        'host': config.PROXY_HOST,
        'port': config.PROXY_PORT,
        'type': config.PROXY_TYPE
    })


def _is_valid_proxy_host(host):
    if not isinstance(host, str) or not host or re.search(r'[@/\\\s]', host):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        if re.fullmatch(r'[0-9.]+', host):
            return False
    if len(host) > 253:
        return False
    return all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
               for label in host.removesuffix('.').split('.'))


@app.route('/api/douyin-cookie', methods=['GET'])
def get_douyin_cookie_config():
    """获取 Cookie 配置状态与健康状态，不返回 Cookie 明文。"""
    cookie = config.DOUYIN_COOKIE or ''
    return jsonify({
        'configured': bool(cookie),
        'length': len(cookie),
        'health': cookie_health_service.snapshot()
    })


@app.route('/api/douyin-cookie', methods=['POST'])
def update_douyin_cookie_config():
    """动态更新抖音 Cookie，并同步到运行中的监控实例。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('cookie', ''), str):
        return jsonify({'error': 'Cookie 配置必须为对象且 cookie 必须为字符串'}), 400
    cookie = _normalize_cookie(data.get('cookie', ''))
    reconnect_active = data.get('reconnect_active', True)

    if cookie and len(cookie) > 50000:
        return jsonify({'error': 'Cookie 过长'}), 400

    config.DOUYIN_COOKIE = cookie
    persisted = True
    persist_error = None
    try:
        update_env_value('DOUYIN_COOKIE', cookie)
    except Exception:
        persisted = False
        persist_error = 'Cookie 保存失败，仅在当前运行时生效'
        logger.exception('写入 .env 失败，Cookie 仅在当前运行时生效')
    updated_rooms = room_manager.update_douyin_cookie(cookie, reconnect_active=reconnect_active)
    # 立即测活：粘贴完 Cookie 马上知道是否有效（清空时返回 unconfigured）
    health = cookie_health_service.run_probe(trigger='cookie_updated', skip_debounce=True)

    logger.info(f"抖音 Cookie 已更新: configured={bool(cookie)}, updated_rooms={updated_rooms}, reconnect_active={reconnect_active}, persisted={persisted}")

    return jsonify({
        'success': True,
        'configured': bool(cookie),
        'length': len(cookie),
        'updated_rooms': updated_rooms,
        'reconnect_active': reconnect_active,
        'persisted': persisted,
        'persist_error': persist_error,
        'health': health
    })


@app.route('/api/douyin-cookie/check', methods=['POST'])
def check_douyin_cookie():
    """手动触发一次 Cookie 测活，同步返回健康状态（跳过防抖）。"""
    snapshot = cookie_health_service.run_probe(trigger='manual', skip_debounce=True)
    return jsonify(snapshot)


# ==================== Socket.IO事件 ====================

@socketio.on('connect')
def handle_connect():
    """客户端连接"""
    if not is_authenticated():
        logger.warning(f"拒绝未登录 Socket.IO 连接: {request.sid}")
        return False
    logger.debug(f"客户端连接: {request.sid}")


@socketio.on('disconnect')
def handle_disconnect():
    """客户端断开"""
    logger.debug(f"客户端断开: {request.sid}")


@socketio.on('join')
def handle_join(data):
    """客户端加入房间"""
    if not is_authenticated() or not isinstance(data, dict):
        return
    live_id = data.get('live_id')
    if isinstance(live_id, str) and re.fullmatch(r'[A-Za-z0-9_.\-]{1,50}', live_id):
        join_room(f'room_{live_id}')
        logger.debug(f"客户端 {request.sid} 加入房间 {live_id}")
        emit('joined', {'live_id': live_id})

        # 主动推送当前统计数据给刚加入的客户端
        monitored_room = room_manager.active_rooms.get(live_id)
        if monitored_room:
            # 房间正在监控，推送实时数据
            rank_list = monitored_room.get_contribution_rank(100)

            # 获取当前场次数据
            current_session_data = None
            if monitored_room.fetcher and monitored_room.fetcher.current_session_id:
                session = data_service.get_current_live_session(live_id)
                if session:
                    current_session_data = {
                        'id': session.id,
                        'start_time': session.start_time.isoformat() if session.start_time else None,
                        'end_time': session.end_time.isoformat() if session.end_time else None,
                        'status': session.status,
                        'total_income': session.total_income,
                        'total_gift_count': session.total_gift_count,
                        'total_chat_count': session.total_chat_count,
                        'total_like_count': session.total_like_count,
                        'peak_viewer_count': session.peak_viewer_count
                    }

            # 获取房间状态
            room = data_service.get_live_room(live_id)
            room_status = room.status if room else None
            room_error_message = room.error_message if room else None

            emit(f'room_{live_id}_stats', {
                'room_status': room_status,
                'room_error_message': room_error_message,
                'current_user_count': monitored_room.stats['current_user_count'],
                'total_user_count': monitored_room.stats['total_user_count'],
                'total_like_count': monitored_room.stats.get('total_like_count', 0),
                'total_income': monitored_room.stats['total_income'],
                'contributor_count': monitored_room.stats['contributor_count'],
                'contributor_info': rank_list,
                'like_rank_list': monitored_room.get_like_rank(100),
                'current_session': current_session_data
            })
            logger.debug(f"推送统计数据给加入的客户端: room_{live_id}")


# ==================== 应用启动和关闭 ====================

def auto_start_24h_rooms():
    """后台自动启动所有 24 小时监控房间。"""
    # 自动启动所有24小时监控房间
    rooms_24h = data_service.get_24h_monitor_rooms()
    for room in rooms_24h:
        room_id = room_manager.add_room(room.live_id, monitor_type='24h', auto_reconnect=True)
        if room_id:
            room_manager.start_room(room_id)
            logger.info(f"自动启动24小时监控: {room.live_id}")


def register_scheduler_jobs():
    """幂等注册应用自定义任务，直接启动和 WSGI 共用此入口。"""
    with _scheduler_jobs_lock:
        if scheduler_service.scheduler.get_job('cookie_health_tick') is None:
            scheduler_service.add_job(
                cookie_health_service.tick,
                'interval',
                seconds=COOKIE_HEALTH_TICK_INTERVAL,
                id='cookie_health_tick',
                name='Cookie健康检查',
                next_run_time=get_china_now(),
            )


register_scheduler_jobs()


def initialize_once_async():
    """首次普通请求时异步执行初始化，避免页面请求被监控启动阻塞。"""
    if app.config.get('TESTING') or scheduler_service.scheduler.running:
        return

    if getattr(app, '_initialized', False):
        return

    with initialization_lock:
        if getattr(app, '_initialized', False):
            return
        register_scheduler_jobs()
        # SchedulerService.start 注册基础任务并后台启动 24h 房间。
        scheduler_service.start()
        app._initialized = True


# 初始化API路由
rooms_bp = init_rooms_api(data_service, room_manager, socketio)
app.register_blueprint(rooms_bp)


@app.before_request
def initialize():
    """每个请求前检查初始化并执行登录保护。"""
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        if 'Origin' in request.headers or 'Referer' in request.headers:
            source = request.headers.get('Origin') or request.headers.get('Referer') or ''
            try:
                parsed = urlparse(source)
                same_host = (parsed.scheme in ('http', 'https')
                             and parsed.netloc.lower() == request.host.lower()
                             and parsed.username is None and parsed.password is None)
            except ValueError:
                same_host = False
            if not same_host:
                return jsonify({'error': '请求来源不匹配'}), 403

    if request.endpoint == 'healthz':
        return None

    if request.endpoint in _PUBLIC_STATIC_ENDPOINTS:
        return None

    if not config.AUTH_REQUIRED or is_authenticated():
        initialize_once_async()
        return None

    allowed_endpoints = {'login', 'static', 'favicon'}
    if request.endpoint in allowed_endpoints:
        return None

    if request.method == 'OPTIONS':
        return None

    if request.path.startswith('/api/'):
        return jsonify({'error': '未登录'}), 401

    return redirect(url_for('login', next=request.full_path if request.query_string else request.path))


def shutdown_application():
    """所有退出路径均执行一次资源清理，单项失败不妨碍后续释放。"""
    global _shutdown_complete
    with _shutdown_lock:
        if _shutdown_complete:
            return
        _shutdown_complete = True
        logger.info('正在关闭应用...')
        callbacks = [stop_status_display, room_manager.shutdown]
        if scheduler_service.scheduler.running:
            callbacks.append(scheduler_service.stop)
        callbacks.append(data_service.close_session)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.exception('应用退出时资源清理失败')


def handle_termination_signal(signum, frame):
    logger.info(f'收到退出信号: {signum}')
    shutdown_application()
    raise SystemExit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, handle_termination_signal)
    signal.signal(signal.SIGINT, handle_termination_signal)
    try:
        register_scheduler_jobs()

        # 启动调度服务
        scheduler_service.start()

        # 启动终端状态面板
        start_status_display()

        logger.info("抖音直播监控平台启动中...")
        logger.info(f"数据库: {make_url(config.DATABASE_URL).render_as_string(hide_password=True)}")
        if not config.AUTH_REQUIRED:
            logger.warning('AUTH_USERNAME/AUTH_PASSWORD 未配置，登录保护已关闭')

        # 运行Flask应用
        socketio.run(
            app,
            debug=config.DEBUG,
            host='0.0.0.0',
            port=int(os.environ.get('APP_PORT', '7654')),
            allow_unsafe_werkzeug=True
        )
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
    finally:
        shutdown_application()
        logger.info("应用已关闭")
