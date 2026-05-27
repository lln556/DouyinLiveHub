"""
抖音直播监控平台 - Flask应用入口
支持多直播间24小时监控、数据持久化存储
"""
import os
import secrets
import threading
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room
from sqlalchemy import inspect as sa_inspect, text

import config
from models.database import Base
from services.data_service import DataService
from services.room_manager import RoomManager, MonitoredRoom
from services.scheduler_service import SchedulerService
from api.rooms import init_rooms_api
from utils.logger import get_logger
from utils.status_display import StatusDisplay

# 使用 loguru 全局日志
logger = get_logger("app")
initialization_lock = threading.Lock()

# 创建Flask应用
app = Flask(__name__)
app.config['SECRET_KEY'] = config.SECRET_KEY

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
    ]
    with engine.connect() as conn:
        for table, column, col_type in migrations:
            columns = [c['name'] for c in inspector.get_columns(table)]
            if column not in columns:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {col_type}'))
                logger.info(f"数据库迁移: {table} 添加列 {column} ({col_type})")
        conn.commit()

migrate_database(data_service.engine)

# 初始化房间管理器
room_manager = RoomManager(data_service, socketio)

# 初始化调度服务
scheduler_service = SchedulerService(room_manager, data_service)

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
    env_path = config.BASE_DIR / '.env'
    escaped_value = value.replace('\\', '\\\\').replace('"', '\\"')
    line = f'{key}="{escaped_value}"\n'
    if not env_path.exists():
        env_path.write_text(line, encoding='utf-8')
        return

    lines = env_path.read_text(encoding='utf-8').splitlines(keepends=True)
    updated = False
    for index, current in enumerate(lines):
        if current.startswith(f'{key}='):
            lines[index] = line
            updated = True
            break
    if not updated:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append(line)
    env_path.write_text(''.join(lines), encoding='utf-8')


# ==================== 路由定义 ====================

def is_authenticated():
    """检查当前请求是否已登录。"""
    if not config.AUTH_REQUIRED:
        return True
    return session.get('authenticated') is True


def get_safe_next(default='/'):
    """只允许站内跳转，避免登录后开放重定向。"""
    next_url = request.args.get('next') or request.form.get('next') or default
    parsed = urlparse(next_url)
    if parsed.netloc or parsed.scheme or not next_url.startswith('/'):
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
        username_ok = secrets.compare_digest(username, config.AUTH_USERNAME)
        password_ok = secrets.compare_digest(password, config.AUTH_PASSWORD)
        if username_ok and password_ok:
            session.clear()
            session['authenticated'] = True
            session['username'] = username
            return redirect(get_safe_next())
        error = '账号或密码错误'

    return render_template('login.html', error=error, next_url=get_safe_next())


@app.route('/logout')
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
    data = request.get_json()
    enabled = data.get('enabled', False)

    # 更新运行时配置
    config.PROXY_ENABLED = enabled
    if 'host' in data:
        config.PROXY_HOST = data['host']
    if 'port' in data:
        config.PROXY_PORT = int(data['port'])
    if 'type' in data:
        config.PROXY_TYPE = data['type']

    logger.info(f"代理配置已更新: enabled={enabled}, host={config.PROXY_HOST}, port={config.PROXY_PORT}")

    return jsonify({
        'success': True,
        'enabled': config.PROXY_ENABLED,
        'host': config.PROXY_HOST,
        'port': config.PROXY_PORT,
        'type': config.PROXY_TYPE
    })


@app.route('/api/douyin-cookie', methods=['GET'])
def get_douyin_cookie_config():
    """获取 Cookie 配置状态，不返回 Cookie 明文。"""
    cookie = config.DOUYIN_COOKIE or ''
    return jsonify({
        'configured': bool(cookie),
        'length': len(cookie)
    })


@app.route('/api/douyin-cookie', methods=['POST'])
def update_douyin_cookie_config():
    """动态更新抖音 Cookie，并同步到运行中的监控实例。"""
    data = request.get_json() or {}
    cookie = ' '.join((data.get('cookie') or '').splitlines()).strip()
    reconnect_active = data.get('reconnect_active', True)

    if cookie and len(cookie) > 50000:
        return jsonify({'error': 'Cookie 过长'}), 400

    config.DOUYIN_COOKIE = cookie
    persisted = True
    persist_error = None
    try:
        update_env_value('DOUYIN_COOKIE', cookie)
    except Exception as e:
        persisted = False
        persist_error = str(e)
        logger.warning(f"写入 .env 失败，Cookie 仅在当前运行时生效: {e}")
    updated_rooms = room_manager.update_douyin_cookie(cookie, reconnect_active=reconnect_active)

    logger.info(f"抖音 Cookie 已更新: configured={bool(cookie)}, updated_rooms={updated_rooms}, reconnect_active={reconnect_active}, persisted={persisted}")

    return jsonify({
        'success': True,
        'configured': bool(cookie),
        'length': len(cookie),
        'updated_rooms': updated_rooms,
        'reconnect_active': reconnect_active,
        'persisted': persisted,
        'persist_error': persist_error
    })


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
    live_id = data.get('live_id')
    if live_id:
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


def initialize_once_async():
    """首次普通请求时异步执行初始化，避免页面请求被监控启动阻塞。"""
    if scheduler_service.scheduler.running:
        return

    if getattr(app, '_initialized', False):
        return

    with initialization_lock:
        if getattr(app, '_initialized', False):
            return
        app._initialized = True
        thread = threading.Thread(
            target=auto_start_24h_rooms,
            daemon=True,
            name="auto-start-24h-rooms"
        )
        thread.start()


# 初始化API路由
rooms_bp = init_rooms_api(data_service, room_manager, socketio)
app.register_blueprint(rooms_bp)


@app.before_request
def initialize():
    """每个请求前检查初始化并执行登录保护。"""
    if request.endpoint == 'healthz':
        return None

    initialize_once_async()

    if not config.AUTH_REQUIRED or is_authenticated():
        return None

    allowed_endpoints = {'login', 'static', 'favicon'}
    if request.endpoint in allowed_endpoints:
        return None

    if request.method == 'OPTIONS':
        return None

    if request.path.startswith('/api/'):
        return jsonify({'error': '未登录'}), 401

    return redirect(url_for('login', next=request.full_path if request.query_string else request.path))


@socketio.on('shutdown')
def handle_shutdown():
    """关闭应用"""
    logger.info("正在关闭应用...")
    stop_status_display()
    room_manager.shutdown()
    scheduler_service.stop()
    data_service.close_session()


if __name__ == '__main__':
    try:
        # 启动调度服务
        scheduler_service.start()

        # 启动终端状态面板
        start_status_display()

        logger.info("抖音直播监控平台启动中...")
        logger.info(f"数据库: {config.DATABASE_URL}")

        # 运行Flask应用
        socketio.run(
            app,
            debug=config.DEBUG,
            host='0.0.0.0',
            port=7654,
            allow_unsafe_werkzeug=True
        )
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
        stop_status_display()
        room_manager.shutdown()
        scheduler_service.stop()
        data_service.close_session()
    finally:
        stop_status_display()
        logger.info("应用已关闭")
