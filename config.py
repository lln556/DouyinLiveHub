"""
配置文件
抖音直播监控平台配置
"""
import os
import json
import logging
import math
import secrets
import sys
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

# 基础路径
BASE_DIR = Path(__file__).resolve().parent

# 环境变量仍优先用于普通配置，Cookie 在下方单独读取持久化值。
load_dotenv(BASE_DIR / '.env')
logger = logging.getLogger(__name__)


SPECIAL_GIFT_DIAMOND_OVERRIDES = {
    '点点星光': 9,
    '闪烁星河': 99,
    '烈焰跑车': 6000,
    '落日追光': 6000,
    '至尊超跑': 12000,
    '无界超跑': 36000,
}


def parse_gift_price_overrides(raw_value=None) -> dict:
    """合并合法的环境配置；每次返回独立映射，不修改内置价格。"""
    overrides = SPECIAL_GIFT_DIAMOND_OVERRIDES.copy()
    raw_value = os.getenv('GIFT_PRICE_OVERRIDES', '') if raw_value is None else raw_value
    if not raw_value.strip():
        return overrides
    try:
        configured = json.loads(raw_value)
        if not isinstance(configured, dict):
            raise ValueError('expected JSON object')
    except (ValueError, TypeError):
        logger.warning('GIFT_PRICE_OVERRIDES 必须是 JSON 对象，已使用默认礼物价格')
        return overrides
    for name, value in configured.items():
        if (isinstance(name, str) and name.strip()
                and isinstance(value, (int, float)) and not isinstance(value, bool)
                and value >= 0 and math.isfinite(value)):
            overrides[name.strip()] = value
    return overrides


class Config:
    SPECIAL_GIFT_DIAMOND_OVERRIDES = SPECIAL_GIFT_DIAMOND_OVERRIDES
    GIFT_PRICE_OVERRIDES = parse_gift_price_overrides()


GIFT_PRICE_OVERRIDES = Config.GIFT_PRICE_OVERRIDES


def _is_running_in_docker() -> bool:
    """检测当前进程是否运行在容器内。"""
    if Path('/.dockerenv').exists():
        return True
    try:
        cgroup = Path('/proc/1/cgroup').read_text()
        return 'docker' in cgroup or '/lxc/' in cgroup
    except Exception:
        return False


def _get_bool_env(name: str, default: bool) -> bool:
    """读取布尔环境变量；空值视为未配置。"""
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == '':
        return default
    return raw_value.strip().lower() in ('1', 'true', 'yes', 'on')


def _resolve_secret_key() -> str:
    """占位密钥使用私有持久化随机值，避免重启使登录态失效。"""
    configured = os.getenv('SECRET_KEY', '').strip()
    placeholders = {
        'your-secret-key-change-in-production',
        'change-this-secret-key-in-production',
        'your-secret-key',
    }
    if configured and configured not in placeholders:
        return configured

    key_path = BASE_DIR / 'data' / '.secret_key'
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(key_path, 0o600)
        key = key_path.read_text(encoding='utf-8').strip()
        if not key:
            raise RuntimeError('持久化 SECRET_KEY 文件为空，请检查 data/.secret_key')
    else:
        key = secrets.token_hex(32)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as key_file:
            os.fchmod(key_file.fileno(), 0o600)
            key_file.write(key)
            key_file.flush()
            os.fsync(key_file.fileno())
    logger.warning('SECRET_KEY 未配置或使用占位值，已使用 data/.secret_key 持久化密钥')
    return key

# 数据库配置 - MySQL 8.0+
# 格式: mysql+pymysql://用户名:密码@主机/数据库名
DATABASE_URL = os.getenv(
    'DATABASE_URL',
    'mysql+pymysql://root:password@localhost/douyin_live'
)

# Flask配置
SECRET_KEY = _resolve_secret_key()
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax')
SESSION_COOKIE_SECURE = _get_bool_env('SESSION_COOKIE_SECURE', False)
DEBUG = os.getenv('DEBUG', 'False') == 'True'
AUTH_USERNAME = os.getenv('AUTH_USERNAME', '').strip()
AUTH_PASSWORD = os.getenv('AUTH_PASSWORD', '')
AUTH_REQUIRED = bool(AUTH_USERNAME and AUTH_PASSWORD)
# 登录态有效天数：cookie 持久化，关闭浏览器后仍可免密登录
AUTH_SESSION_DAYS = int(os.getenv('AUTH_SESSION_DAYS', '30'))

# SQLAlchemy配置
# 独立于 Flask DEBUG，避免开启 Web 调试时把所有 SQL 查询刷到终端状态面板。
SQLALCHEMY_ECHO = os.getenv('SQLALCHEMY_ECHO', 'False') == 'True'

# 代理配置
PROXY_ENABLED = os.getenv('PROXY_ENABLED', 'False') == 'True'  # 是否启用代理
PROXY_HOST = os.getenv('PROXY_HOST', '127.0.0.1')  # 代理主机
PROXY_PORT = int(os.getenv('PROXY_PORT', '7890'))  # 代理端口
PROXY_TYPE = os.getenv('PROXY_TYPE', 'http')  # 代理类型: http, socks5

# WebSocket配置
SOCKETIO_CORS_ALLOWED_ORIGINS = os.getenv('SOCKETIO_CORS_ALLOWED_ORIGINS') or None
SOCKETIO_LOGGER = os.getenv('SOCKETIO_LOGGER', 'False') == 'True'
SOCKETIO_ENGINEIO_LOGGER = os.getenv('SOCKETIO_ENGINEIO_LOGGER', 'False') == 'True'
SOCKETIO_ASYNC_MODE = os.getenv('SOCKETIO_ASYNC_MODE', 'threading')
WS_HEARTBEAT_INTERVAL = int(os.getenv('WS_HEARTBEAT_INTERVAL', '5'))  # WebSocket心跳间隔(秒)
WS_WATCHDOG_INTERVAL = int(os.getenv('WS_WATCHDOG_INTERVAL', '10'))  # WebSocket看门狗检查间隔(秒)
WS_CONNECT_TIMEOUT = int(os.getenv('WS_CONNECT_TIMEOUT', '60'))  # WebSocket连接建立超时(秒)
WS_DATA_SILENCE_TIMEOUT = int(os.getenv('WS_DATA_SILENCE_TIMEOUT', '60'))  # WebSocket无数据超时(秒)
WS_BUSINESS_WATCHDOG_ENABLED = os.getenv('WS_BUSINESS_WATCHDOG_ENABLED', 'False') == 'True'
WS_BUSINESS_SILENCE_TIMEOUT = int(os.getenv('WS_BUSINESS_SILENCE_TIMEOUT', '300'))  # 无交互消息超时(秒)

# 监控配置
MONITOR_RECONNECT_INTERVAL = int(os.getenv('MONITOR_RECONNECT_INTERVAL', '30'))  # 重连间隔(秒)
MONITOR_MAX_RETRIES = int(os.getenv('MONITOR_MAX_RETRIES', '5'))  # 最大重试次数
MONITOR_RECONNECT_DELAY = int(os.getenv('MONITOR_RECONNECT_DELAY', '30'))  # 重连延迟(秒)
MONITOR_STATUS_POLL_INTERVAL = int(os.getenv('MONITOR_STATUS_POLL_INTERVAL', '60'))  # 轮询直播状态间隔(秒)
MONITOR_OFFLINE_END_THRESHOLD = int(os.getenv('MONITOR_OFFLINE_END_THRESHOLD', '3'))  # 连续确认未开播多少次后结束场次

# 防风控配置
ANTI_DETECTION_ENABLED = os.getenv('ANTI_DETECTION_ENABLED', 'True') == 'True'  # 是否启用防风控机制
ANTI_DETECTION_THREAD_START_INTERVAL = int(os.getenv('ANTI_DETECTION_THREAD_START_INTERVAL', '3'))  # 线程启动间隔(秒)
ANTI_DETECTION_QUEUE_READ_DELAY = int(os.getenv('ANTI_DETECTION_QUEUE_READ_DELAY', '2'))  # 排队读取网址时间(秒)
ANTI_DETECTION_JITTER_ENABLED = os.getenv('ANTI_DETECTION_JITTER_ENABLED', 'True') == 'True'  # 启用检测周期抖动
ANTI_DETECTION_JITTER_RANGE = int(os.getenv('ANTI_DETECTION_JITTER_RANGE', '5'))  # 抖动范围(+/-秒)

# 数据保留配置
DATA_RETENTION_DAYS = int(os.getenv('DATA_RETENTION_DAYS', '90'))  # 数据保留天数，0表示永久保留

# 日志配置
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_FILE = str(BASE_DIR / 'logs' / 'app.log')
LOG_ROTATION = os.getenv('LOG_ROTATION', '10 MB')
LOG_RETENTION = os.getenv('LOG_RETENTION', '14 days')
LOG_COMPRESSION = os.getenv('LOG_COMPRESSION', 'zip')
ERROR_LOG_ROTATION = os.getenv('ERROR_LOG_ROTATION', '5 MB')
STATUS_DISPLAY_ENABLED = _get_bool_env('STATUS_DISPLAY_ENABLED', not _is_running_in_docker())

# 调度器配置
SCHEDULER_RESTART_FAILED_INTERVAL = int(os.getenv('SCHEDULER_RESTART_FAILED_INTERVAL', '30'))  # 检查失败房间间隔(秒)
SCHEDULER_STATS_SNAPSHOT_INTERVAL = int(os.getenv('SCHEDULER_STATS_SNAPSHOT_INTERVAL', '60'))  # 保存统计快照间隔(秒)
SCHEDULER_CLEANUPOldData_INTERVAL = int(os.getenv('SCHEDULER_CLEANUPOldData_INTERVAL', '3600'))  # 清理旧数据间隔(秒)

# Cookie 健康检测配置
COOKIE_HEALTH_CHECK_INTERVAL = int(os.getenv('COOKIE_HEALTH_CHECK_INTERVAL', '1800'))  # 定时探测间隔(秒)，0=关闭定时探测
COOKIE_HEALTH_GIFT_SILENCE = int(os.getenv('COOKIE_HEALTH_GIFT_SILENCE', '1800'))  # 被动信号: 无礼物多久算可疑(秒)
COOKIE_HEALTH_CHAT_ACTIVE = int(os.getenv('COOKIE_HEALTH_CHAT_ACTIVE', '600'))  # 被动信号: 多久内有弹幕算活跃(秒)

# WebSocket配置
WS_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
WS_HOST = "https://www.douyin.com/"
WS_LIVE_URL = "https://live.douyin.com/"

# 抖音登录 Cookie
# 2026-05 起，游客身份可能收不到礼物推送；如需礼物消息，请配置已登录 live.douyin.com 后取得的完整 Cookie。
DOUYIN_COOKIE = os.getenv('DOUYIN_COOKIE', '').strip()
if 'pytest' not in sys.modules:
    # python-dotenv 即使使用单引号也展开 ${...}，Cookie 必须按字面值解析。
    persisted_cookie = dotenv_values(BASE_DIR / '.env', interpolate=False).get('DOUYIN_COOKIE')
    if persisted_cookie and persisted_cookie.strip():
        DOUYIN_COOKIE = persisted_cookie.strip()

# 确保必要的目录存在
os.makedirs(BASE_DIR / 'data', exist_ok=True)
os.makedirs(BASE_DIR / 'logs', exist_ok=True)


def get_proxy_config():
    """获取代理配置"""
    if not PROXY_ENABLED:
        return None
    proxy_url = f"{PROXY_TYPE}://{PROXY_HOST}:{PROXY_PORT}"
    return {
        'http': proxy_url,
        'https': proxy_url
    }


def get_proxy_url():
    """获取代理URL（用于WebSocket）"""
    if not PROXY_ENABLED:
        return None
    return f"{PROXY_TYPE}://{PROXY_HOST}:{PROXY_PORT}"
