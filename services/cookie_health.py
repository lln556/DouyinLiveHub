"""
Cookie 健康检测服务
主动探测抖音登录态是否有效，结合被动信号（有弹幕但长时间无礼物）
判断 Cookie 是否失活。状态切换写入 system_events 全局事件。
"""
import threading
import time
from dataclasses import dataclass

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


def probe_douyin_cookie(cookie: str, proxies: dict = None) -> CookieProbeResult:
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
