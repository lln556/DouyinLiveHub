"""
全局 Loguru 日志模块
提供统一的日志记录功能，支持房间上下文绑定

控制台仅输出 WARNING 及以上级别（减少噪音，让状态面板占主导）
文件日志级别由 LOG_LEVEL 控制，默认 INFO

当 StatusDisplay 激活后，控制台输出通过 rich.Console 路由，
WARNING/ERROR 消息会渲染在状态面板上方，不会破坏面板布局。
"""
import sys
from pathlib import Path
from loguru import logger
from rich.text import Text

import config

# 移除默认的 handler
logger.remove()

# 共享的 rich Console 引用（由 StatusDisplay 设置）
_console = None


def set_console(console):
    """设置 rich Console 实例，供控制台日志输出使用"""
    global _console
    _console = console


# 日志格式定义
# 控制台格式（带颜色，用于 loguru colorize）
CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <7}</level> | "
    "<cyan>[{extra[room_id]}]</cyan> "
    "<blue>{extra[module]}</blue> | "
    "<level>{message}</level>"
)

# 纯文本控制台格式（用于 rich Console 路由时，不含 loguru 颜色标签）
CONSOLE_PLAIN_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss} | "
    "{level: <7} | "
    "[{extra[room_id]}] "
    "{extra[module]} | "
    "{message}"
)

# 文件格式（纯文本，便于检索）
FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss} | "
    "{level: <7} | "
    "[{extra[room_id]}] "
    "{extra[module]} | "
    "{message}"
)

# 级别对应的 rich 样式
_LEVEL_STYLES = {
    'TRACE': 'dim',
    'DEBUG': 'dim cyan',
    'INFO': 'green',
    'SUCCESS': 'bold green',
    'WARNING': 'bold yellow',
    'ERROR': 'bold red',
    'CRITICAL': 'bold white on red',
}


def _console_sink(message):
    """
    自定义 loguru sink：通过 rich Console 输出日志。
    当 rich.Live 处于活跃状态时，console.print() 会自动将输出渲染到面板上方。
    当 Console 未设置时，回退到 sys.stderr。
    """
    if _console is not None:
        record = message.record
        level_name = record['level'].name
        style = _LEVEL_STYLES.get(level_name, '')
        text = str(message).rstrip('\n')
        _console.print(Text(text, style=style))
    else:
        sys.stderr.write(str(message))


def setup_logger():
    """
    配置全局日志器
    """
    # 确保日志目录存在
    log_dir = Path(config.BASE_DIR) / 'logs'
    log_dir.mkdir(exist_ok=True)

    # 控制台输出（WARNING 及以上，通过 rich Console 路由）
    logger.add(
        _console_sink,
        format=CONSOLE_PLAIN_FORMAT,
        level="WARNING",
        colorize=False,
        filter=lambda record: record["extra"].get("room_id") is not None
    )

    # 全局日志文件（所有级别）
    logger.add(
        log_dir / "app_{time:YYYY-MM-DD}.log",
        format=FILE_FORMAT,
        level=config.LOG_LEVEL,
        rotation=config.LOG_ROTATION,
        retention=config.LOG_RETENTION,
        compression=config.LOG_COMPRESSION or None,
        encoding="utf-8",
        filter=lambda record: record["extra"].get("room_id") is not None
    )

    # 错误日志单独记录
    logger.add(
        log_dir / "error_{time:YYYY-MM-DD}.log",
        format=FILE_FORMAT,
        level="ERROR",
        rotation=config.ERROR_LOG_ROTATION,
        retention=config.LOG_RETENTION,
        compression=config.LOG_COMPRESSION or None,
        encoding="utf-8",
        filter=lambda record: record["extra"].get("room_id") is not None
    )

    return logger


def get_logger(module: str = "global", room_id: str = "全局"):
    """
    获取带上下文的日志器

    :param module: 模块名称
    :param room_id: 房间ID，默认为"全局"
    :return: 绑定上下文的日志器

    使用示例:
        log = get_logger("liveMan", "123456")
        log.info("连接成功")
        # 输出: 2026-01-31 11:00:00 | INFO    | [123456] liveMan | 连接成功
    """
    return logger.bind(module=module, room_id=room_id)


# 初始化日志配置
setup_logger()

# 导出默认日志器（用于全局日志）
log = get_logger()
