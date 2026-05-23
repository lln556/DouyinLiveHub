"""
终端状态面板
使用 rich.Live + rich.Table 动态刷新显示所有房间状态

Docker 环境下自动禁用 rich 面板，改用定期文本输出
"""
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from wcwidth import wcswidth

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from models.database import CHINA_TZ


def _is_docker() -> bool:
    """检测是否在 Docker 容器中运行"""
    # 方法1: 检查 /.dockerenv 文件
    if Path("/.dockerenv").exists():
        return True
    # 方法2: 检查 /proc/1/cgroup 是否包含 docker
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
        if "docker" in cgroup or "/lxc/" in cgroup:
            return True
    except Exception:
        pass
    return False


# 检测环境
_IS_DOCKER = _is_docker()

# Docker 环境下禁用 rich，使用文本模式
if _IS_DOCKER:
    # Docker 环境不使用 rich Console，日志直接输出到 stderr
    console = None
    _RICH_MODE = False
else:
    # 非 Docker 环境，使用 rich
    console = Console(stderr=True)
    _RICH_MODE = True

# ANSI 转义码
ANSI_CLEAR = "\033[2J\033[H"  # 清屏 + 光标移到左上角
ANSI_EL = "\033[K"  # 清除到行尾

# 状态颜色映射
STATUS_STYLES = {
    'monitoring': 'bold green',
    'offline': 'yellow',
    'waiting': 'yellow',
    'error': 'bold red',
    'stopped': 'dim',
}

# 状态中文映射
STATUS_LABELS = {
    'monitoring': '监控中',
    'offline': '轮询中',
    'waiting': '等待中',
    'error': '错误',
    'stopped': '已停止',
}

# 直播状态映射
LIVE_STATUS_LABELS = {
    'monitoring': '直播中',
    'offline': '未开播',
    'waiting': '未开播',
    'error': '未知',
    'stopped': '未开播',
}


def _pad_to_width(text: str, width: int, align: str = 'center') -> str:
    """
    将文本填充到指定显示宽度（考虑中文字符占2列）

    :param text: 原文本
    :param width: 目标显示宽度（列数）
    :param align: 对齐方式 'left', 'center', 'right'
    :return: 填充后的文本
    """
    display_width = wcswidth(text)
    if display_width >= width:
        # 超宽则截断
        truncated = text
        while wcswidth(truncated) > width and truncated:
            truncated = truncated[:-1]
        return truncated

    padding = width - display_width
    if align == 'left':
        return text + ' ' * padding
    elif align == 'right':
        return ' ' * padding + text
    else:  # center
        left_pad = padding // 2
        right_pad = padding - left_pad
        return ' ' * left_pad + text + ' ' * right_pad


class StatusDisplay:
    """终端状态面板，Docker 环境使用文本模式，本地环境使用 rich.Live"""

    def __init__(self, room_manager, refresh_interval: float = 5.0):
        """
        :param room_manager: RoomManager 实例
        :param refresh_interval: 刷新间隔（秒）
        """
        self.room_manager = room_manager
        self.refresh_interval = refresh_interval
        self._thread = None
        self._stop_event = threading.Event()
        self._live = None
        self._rich_mode = _RICH_MODE
        self._first_run = True  # 首次运行标志

        if _IS_DOCKER:
            sys.stderr.write("[StatusDisplay] Docker 环境检测，使用文本模式输出状态\n")

    def _build_table(self) -> Table:
        """构建状态表格"""
        now = datetime.now(CHINA_TZ).strftime('%Y-%m-%d %H:%M:%S')

        table = Table(
            title=f"抖音直播监控平台 | 运行中 | {now}",
            title_style="bold cyan",
            border_style="bright_black",
            show_lines=False,
        )

        table.add_column("主播", style="bold", min_width=12, max_width=20, no_wrap=True)
        table.add_column("live_id", min_width=13, max_width=15, no_wrap=True)
        table.add_column("监控状态", justify="center", min_width=8)
        table.add_column("直播", justify="center", min_width=6)
        table.add_column("在线", justify="right", min_width=6)
        table.add_column("收入", justify="right", min_width=8)
        table.add_column("备注", max_width=24, no_wrap=True)

        # 获取所有房间状态
        display_rows = self._get_display_data()

        if not display_rows:
            table.add_row(
                Text("暂无监控房间", style="dim italic"),
                "", "", "", "", "", ""
            )
            return table

        for row in display_rows:
            status = row.get('status', 'stopped')
            style = STATUS_STYLES.get(status, 'dim')
            status_label = STATUS_LABELS.get(status, status)
            live_label = LIVE_STATUS_LABELS.get(status, '未知')

            # 在线人数
            viewer_count = row.get('current_user_count', 0)
            if status == 'monitoring' and viewer_count > 0:
                viewer_str = f"{viewer_count:,}"
            else:
                viewer_str = "-"

            # 收入
            total_income = row.get('total_income', 0)
            if total_income > 0:
                income_str = f"{total_income:,.0f}"
            else:
                income_str = "-"

            # 备注（错误信息等）
            note = row.get('note', '')

            table.add_row(
                Text(row.get('anchor_name', '未知'), style=style),
                Text(row.get('live_id', ''), style="dim"),
                Text(status_label, style=style),
                Text(live_label, style="green" if status == 'monitoring' else "dim"),
                Text(viewer_str, style=style),
                Text(income_str, style=style),
                Text(note, style="yellow" if note else "dim"),
            )

        return table

    def _get_display_data(self) -> list:
        """获取所有房间的显示数据"""
        rows = []
        try:
            data_service = self.room_manager.data_service

            # 获取数据库中所有房间
            all_rooms = data_service.list_live_rooms()

            for room in all_rooms:
                live_id = room.live_id
                row = {
                    'anchor_name': room.anchor_name or live_id,
                    'live_id': live_id,
                    'status': room.status or 'stopped',
                    'current_user_count': 0,
                    'total_income': 0,
                    'note': '',
                }

                # 如果房间在活跃列表中，获取实时统计
                monitored = self.room_manager.active_rooms.get(live_id)
                if monitored:
                    row['current_user_count'] = monitored.stats.get('current_user_count', 0)
                    row['total_income'] = monitored.stats.get('total_income', 0)

                # 错误信息（首次运行不显示状态检测失败提示）
                if room.status == 'error' and room.error_message:
                    row['note'] = room.error_message[:24]
                elif room.status in ('offline', 'waiting') and room.error_message:
                    # 首次运行不显示状态检测失败提示
                    if self._first_run and "直播间状态检测失败" in room.error_message:
                        row['note'] = "初始化中..."
                    else:
                        row['note'] = room.error_message[:24]

                rows.append(row)
        except Exception:
            pass

        return rows

    def _print_text_status(self):
        """文本模式：打印状态列表（Docker 环境使用）"""
        display_rows = self._get_display_data()
        if not display_rows:
            return

        now = datetime.now(CHINA_TZ).strftime('%Y-%m-%d %H:%M:%S')

        # 使用 ANSI 转义码清屏并重绘
        sys.stderr.write(ANSI_CLEAR)
        sys.stderr.flush()

        # 标题
        sys.stderr.write(f"抖音直播监控平台 | 运行中 | {now}\n")
        sys.stderr.write("=" * 115 + "\n")

        # 表头（使用显示宽度填充）
        header = (
            _pad_to_width('主播', 20, 'center') + " | " +
            _pad_to_width('live_id', 15, 'center') + " | " +
            _pad_to_width('监控状态', 8, 'center') + " | " +
            _pad_to_width('直播', 6, 'center') + " | " +
            _pad_to_width('在线人数', 8, 'center') + " | " +
            _pad_to_width('收入', 10, 'center') + " | " +
            _pad_to_width('备注', 30, 'center')
        )
        sys.stderr.write(header + "\n")
        sys.stderr.write("-" * 115 + "\n")

        # 数据行
        for row in display_rows:
            status = row.get('status', 'stopped')
            status_label = STATUS_LABELS.get(status, status)
            live_label = LIVE_STATUS_LABELS.get(status, '未知')

            viewer_count = row.get('current_user_count', 0)
            if status == 'monitoring' and viewer_count > 0:
                viewer_str = f"{viewer_count:,}"
            else:
                viewer_str = "-"

            total_income = row.get('total_income', 0)
            if total_income > 0:
                income_str = f"{total_income:,.0f}"
            else:
                income_str = "-"

            note = row.get('note', '')
            anchor = row.get('anchor_name', '未知')
            live_id = row.get('live_id', '')

            line = (
                _pad_to_width(anchor, 20, 'center') + " | " +
                _pad_to_width(live_id, 15, 'center') + " | " +
                _pad_to_width(status_label, 8, 'center') + " | " +
                _pad_to_width(live_label, 6, 'center') + " | " +
                _pad_to_width(viewer_str, 8, 'center') + " | " +
                _pad_to_width(income_str, 10, 'center') + " | " +
                _pad_to_width(note, 30, 'left')
            )
            sys.stderr.write(line + "\n")

        sys.stderr.write("=" * 115 + "\n")
        sys.stderr.flush()

    def _run(self):
        """后台线程：刷新状态显示"""
        if self._rich_mode:
            # Rich 模式：使用 rich.Live 动态刷新
            try:
                with Live(
                    self._build_table(),
                    console=console,
                    refresh_per_second=0.5,
                    transient=False,
                ) as live:
                    self._live = live
                    while not self._stop_event.is_set():
                        live.update(self._build_table())
                        self._stop_event.wait(self.refresh_interval)
                    self._live = None
            except Exception:
                self._live = None
        else:
            # 文本模式：定期刷新状态（Docker 环境）
            while not self._stop_event.is_set():
                self._print_text_status()
                self._stop_event.wait(self.refresh_interval)
                self._first_run = False  # 首次运行后更新标志

    def start(self):
        """启动状态面板"""
        if self._thread and self._thread.is_alive():
            return

        # 只在 rich 模式下注册 Console 到日志模块
        # 文本模式（Docker）下日志直接输出到 stderr，不需要通过 rich
        if self._rich_mode and console:
            from utils.logger import set_console
            set_console(console)

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="status-display")
        self._thread.start()

    def stop(self):
        """停止状态面板"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
