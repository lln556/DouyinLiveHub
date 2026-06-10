"""
Cookie 健康检测服务
主动探测抖音登录态是否有效，结合被动信号（有弹幕但长时间无礼物）
判断 Cookie 是否失活。状态切换写入 system_events 全局事件。
"""
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

import config
from models.database import get_china_now
from utils.logger import get_logger

logger = get_logger("cookie_health")

# 登录态自检接口（判定字段以 scripts/check_cookie.sh 实测为准）
PROBE_URL = 'https://live.douyin.com/webcast/user/me/?aid=6383&device_platform=web'

# tick 调度间隔(秒)：每次 tick 评估被动信号 + 判断是否到达定时探测时间
COOKIE_HEALTH_TICK_INTERVAL = 300
# 被动信号触发探测的最小间隔(秒)
PASSIVE_PROBE_MIN_INTERVAL = 900


@dataclass
class CookieProbeResult:
    """单次探测结果。outcome: alive(登录态有效) / dead(明确未登录) / inconclusive(无法判定)"""
    outcome: str
    detail: str


def probe_douyin_cookie(cookie: str, proxies: Optional[dict] = None) -> CookieProbeResult:
    """
    用给定 Cookie 请求抖音登录态自检接口，判定登录态是否有效。
    网络错误、5xx、响应不可解析 → inconclusive（不可作为失活证据）。
    """
    headers = {
        'User-Agent': config.WS_USER_AGENT,
        'Referer': 'https://live.douyin.com/',
        'Cookie': cookie,
    }
    try:
        resp = requests.get(PROBE_URL, headers=headers, proxies=proxies, timeout=(5, 15))
    except requests.RequestException as e:
        return CookieProbeResult('inconclusive', f'网络请求失败: {e}')

    if resp.status_code != 200:
        return CookieProbeResult('inconclusive', f'HTTP {resp.status_code}')

    try:
        payload = resp.json()
    except ValueError:
        return CookieProbeResult('inconclusive', '响应不是有效 JSON')

    status_code = payload.get('status_code')
    user = payload.get('data') or {}
    user_id = str(user.get('id_str') or '')
    if status_code == 0 and user_id not in ('', '0'):
        return CookieProbeResult('alive', f'登录用户 id={user_id}')
    return CookieProbeResult('dead', f'未检测到登录态 (status_code={status_code})')


class CookieHealthService:
    """
    Cookie 健康状态机（内存态，重启后回到初始状态由首次探测刷新）。
    状态: unconfigured / unknown / healthy / suspect / dead
    """

    def __init__(self, room_manager, data_service):
        self.room_manager = room_manager
        self.data_service = data_service
        self._lock = threading.Lock()
        self.status = 'unknown' if config.DOUYIN_COOKIE else 'unconfigured'
        self.last_check_time = None
        self.last_ok_time = None
        self.last_error = None
        self.last_trigger = None
        self.fail_count = 0
        self._last_probe_at = None  # time.time()，tick 调度与被动信号限流用

    def snapshot(self) -> dict:
        """当前健康状态快照（API 返回值）。"""
        return {
            'status': self.status,
            'last_check_time': self.last_check_time,
            'last_ok_time': self.last_ok_time,
            'last_error': self.last_error,
            'trigger': self.last_trigger,
        }

    def run_probe(self, trigger: str, skip_debounce: bool = False) -> dict:
        """
        执行一次主动探测并推进状态机。
        :param trigger: scheduled / passive / manual / cookie_updated
        :param skip_debounce: True 时单次明确未登录即判 dead（人工确认场景）
        """
        with self._lock:
            cookie = config.DOUYIN_COOKIE
            if not cookie:
                self._set_unconfigured()
                return self.snapshot()

            self._last_probe_at = time.time()
            result = probe_douyin_cookie(cookie, config.get_proxy_config())
            now_str = get_china_now().strftime('%Y-%m-%d %H:%M:%S')
            self.last_check_time = now_str
            self.last_trigger = trigger
            logger.info(f"Cookie 探测完成: trigger={trigger}, outcome={result.outcome}, detail={result.detail}")

            if result.outcome == 'alive':
                self.fail_count = 0
                self.last_error = None
                self.last_ok_time = now_str
                self._transition('healthy', trigger, result.detail)
            elif result.outcome == 'dead':
                self.fail_count += 1
                self.last_error = result.detail
                if skip_debounce or self.fail_count >= 2:
                    self._transition('dead', trigger, result.detail)
                else:
                    self._transition('suspect', trigger, result.detail)
            else:  # inconclusive: 不可作为失活证据，只记录错误
                self.last_error = result.detail
            return self.snapshot()

    def _set_unconfigured(self):
        self.status = 'unconfigured'
        self.fail_count = 0
        self.last_error = None

    def _transition(self, new_status: str, trigger: str, detail: str):
        """状态切换；跨越 dead 边界时写全局系统事件。"""
        old = self.status
        if old == new_status:
            return
        self.status = new_status
        if new_status == 'dead':
            self.data_service.log_system_event(
                None, 'cookie_dead',
                message=f'抖音 Cookie 已失活（触发: {trigger}）: {detail}')
            logger.warning(f"抖音 Cookie 已失活: {detail}")
        elif old == 'dead' and new_status == 'healthy':
            self.data_service.log_system_event(
                None, 'cookie_recovered',
                message=f'抖音 Cookie 已恢复（触发: {trigger}）')
            logger.info("抖音 Cookie 已恢复")

    def _passive_signal_hit(self) -> bool:
        """
        被动信号：存在"连接足够久 + 近期有弹幕 + 长时间无礼物"的房间。
        任一房间近期收到礼物则直接否决（Cookie 显然有效）。
        """
        now = time.time()
        hit = False
        for monitored_room in list(self.room_manager.active_rooms.values()):
            fetcher = getattr(monitored_room, 'fetcher', None)
            if fetcher is None:
                continue
            last_gift = getattr(fetcher, 'last_gift_time', None)
            if last_gift and now - last_gift <= config.COOKIE_HEALTH_GIFT_SILENCE:
                return False
            ws_open = getattr(fetcher, 'ws_open_time', None)
            last_chat = getattr(fetcher, 'last_chat_time', None)
            connected_long_enough = ws_open and now - ws_open > config.COOKIE_HEALTH_GIFT_SILENCE
            chat_active = last_chat and now - last_chat <= config.COOKIE_HEALTH_CHAT_ACTIVE
            if connected_long_enough and chat_active:
                hit = True
        return hit

    def tick(self):
        """定时入口（APScheduler 每 COOKIE_HEALTH_TICK_INTERVAL 秒调用一次）。"""
        try:
            if not config.DOUYIN_COOKIE:
                if self.status != 'unconfigured':
                    self._set_unconfigured()
                return
            now = time.time()
            probe_due = (self._last_probe_at is None
                         or now - self._last_probe_at >= config.COOKIE_HEALTH_CHECK_INTERVAL)
            passive_allowed = (self._last_probe_at is None
                               or now - self._last_probe_at >= PASSIVE_PROBE_MIN_INTERVAL)
            if passive_allowed and self._passive_signal_hit():
                self.run_probe(trigger='passive')
            elif config.COOKIE_HEALTH_CHECK_INTERVAL > 0 and probe_due:
                self.run_probe(trigger='scheduled')
        except Exception as e:
            logger.error(f"Cookie 健康检查 tick 出错: {e}")


if __name__ == '__main__':
    # 手动验证入口：打印原始响应 + 判定结果，用于校准判定字段
    import sys

    if not config.DOUYIN_COOKIE:
        print('未配置 DOUYIN_COOKIE')
        sys.exit(1)
    _headers = {
        'User-Agent': config.WS_USER_AGENT,
        'Referer': 'https://live.douyin.com/',
        'Cookie': config.DOUYIN_COOKIE,
    }
    _resp = requests.get(PROBE_URL, headers=_headers, proxies=config.get_proxy_config(), timeout=(5, 15))
    print(f'HTTP {_resp.status_code}')
    print(_resp.text[:2000])
    _result = probe_douyin_cookie(config.DOUYIN_COOKIE, config.get_proxy_config())
    print(f'判定: {_result.outcome} ({_result.detail})')
