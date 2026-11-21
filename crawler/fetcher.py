#!/usr/bin/python
# coding:utf-8

# @FileName:    fetcher.py
# @Time:        2024/1/2 21:51
# @Author:      bubu
# @Project:     DouyinLiveWebFetcher

"""
抖音直播核心爬虫类
"""
import gzip
import hashlib
import json
import os
import queue
import random
import re
import string
import threading
import time
import urllib.parse
from functools import lru_cache

import requests
import websocket
from py_mini_racer import MiniRacer

from .signature import get__ac_signature
from protobuf.douyin import *
from urllib3.util.url import parse_url
from utils.logger import get_logger
import config


# 获取JS文件路径（相对于crawler模块）
JS_DIR = os.path.join(os.path.dirname(__file__), 'js')
_JS_SOURCES = {}
for _js_name in ('a_bogus.js', 'sign.js'):
    _js_path = os.path.join(JS_DIR, _js_name)
    with open(_js_path, 'r', encoding='utf-8') as _js_file:
        _JS_SOURCES[_js_path] = _js_file.read()
_JS_CONTEXTS = threading.local()
_JS_LOCK = threading.Lock()
_JS_GENERATIONS = {}
_JS_QUEUE = queue.Queue()
_JS_WORKER_THREAD = None
# Retained so V8/PartitionAlloc isolates are never finalized on arbitrary GC threads.
_RETIRED_JS_CONTEXTS = []

_STATUS_PROBE_LOCK = threading.Lock()
_LAST_STATUS_PROBE_AT = 0.0
STATUS_PROBE_MIN_INTERVAL = 1.5


def _throttle_status_probe():
    """跨房间错峰发起状态探测，避免同时请求触发空响应风控。"""
    if not config.ANTI_DETECTION_ENABLED:
        return
    global _LAST_STATUS_PROBE_AT
    with _STATUS_PROBE_LOCK:
        elapsed = time.monotonic() - _LAST_STATUS_PROBE_AT
        if elapsed < STATUS_PROBE_MIN_INTERVAL:
            time.sleep(STATUS_PROBE_MIN_INTERVAL - elapsed)
        _LAST_STATUS_PROBE_AT = time.monotonic()


@lru_cache(maxsize=16)
def _read_js_source(js_file: str) -> str:
    if js_file in _JS_SOURCES:
        return _JS_SOURCES[js_file]
    with open(js_file, 'r', encoding='utf-8') as file:
        return file.read()


def _js_worker_loop():
    """在单一专用线程内串行创建并复用 MiniRacer，避免 V8 多线程 PartitionAlloc 崩溃。"""
    ctxs = {}

    def _get_or_create(js_file: str, generation: int):
        key = (js_file, generation)
        ctx = ctxs.get(key)
        if ctx is not None:
            return ctx
        for old_key in [k for k in ctxs if k[0] == js_file and k[1] != generation]:
            _RETIRED_JS_CONTEXTS.append(ctxs.pop(old_key))
        ctx = MiniRacer()
        try:
            ctx.eval(_read_js_source(js_file))
        except Exception:
            _RETIRED_JS_CONTEXTS.append(ctx)
            raise
        ctxs[key] = ctx
        return ctx

    while True:
        js_file, generation, op, payload, resp_queue = _JS_QUEUE.get()
        try:
            ctx = _get_or_create(js_file, generation)
            if op == 'init':
                resp_queue.put((True, None))
            elif op == 'call':
                fn, args = payload
                resp_queue.put((True, ctx.call(fn, *args)))
            elif op == 'eval':
                resp_queue.put((True, ctx.eval(payload)))
            else:
                raise ValueError(f'未知 JS 操作: {op}')
        except Exception as exc:
            bad = ctxs.pop((js_file, generation), None)
            if bad is not None:
                _RETIRED_JS_CONTEXTS.append(bad)
            resp_queue.put((False, exc))


def _ensure_js_worker():
    global _JS_WORKER_THREAD
    with _JS_LOCK:
        if _JS_WORKER_THREAD is None or not _JS_WORKER_THREAD.is_alive():
            worker = threading.Thread(
                target=_js_worker_loop,
                name='mini-racer-worker',
                daemon=True,
            )
            worker.start()
            _JS_WORKER_THREAD = worker


def _dispatch_js(js_file: str, generation: int, op: str, payload=None):
    _ensure_js_worker()
    resp_queue = queue.Queue(maxsize=1)
    _JS_QUEUE.put((js_file, generation, op, payload, resp_queue))
    ok, result = resp_queue.get()
    if not ok:
        raise result
    return result


class _JsContextProxy:
    """暴露与 MiniRacer 兼容的 call/eval 接口，内部转发至单一 V8 工作线程。"""

    def __init__(self, js_file: str, generation: int):
        self.js_file = js_file
        self.generation = generation

    def call(self, function: str, *args):
        return _dispatch_js(self.js_file, self.generation, 'call', (function, args))

    def eval(self, code: str):
        return _dispatch_js(self.js_file, self.generation, 'eval', code)


def execute_js(js_file: str):
    """兼容原入口，按线程和脚本返回固定代理并在单一 V8 线程内复用上下文。"""
    js_file = os.path.abspath(js_file)
    with _JS_LOCK:
        generation = _JS_GENERATIONS.get(js_file, 0)
    contexts = getattr(_JS_CONTEXTS, 'contexts', None)
    if contexts is None:
        contexts = _JS_CONTEXTS.contexts = {}
    proxy = contexts.get(js_file)
    if proxy is None or proxy.generation != generation:
        _dispatch_js(js_file, generation, 'init')
        proxy = _JsContextProxy(js_file, generation)
        contexts[js_file] = proxy
    return proxy


def _call_js(js_file: str, function: str, *args):
    abs_path = os.path.abspath(js_file)
    try:
        return execute_js(js_file).call(function, *args)
    except Exception:
        with _JS_LOCK:
            _JS_GENERATIONS[abs_path] = _JS_GENERATIONS.get(abs_path, 0) + 1
        contexts = getattr(_JS_CONTEXTS, 'contexts', None)
        if isinstance(contexts, dict):
            contexts.pop(abs_path, None)
        raise


def decompress_push_frame_payload(package) -> bytes:
    """兼容 gzip 和未压缩 Protobuf，空载荷不参与解压。"""
    payload = bytes(package.payload or b'')
    if not payload:
        return b''
    compressed = payload[:2] == b'\x1f\x8b' or any(
        header.key == 'compress_type' and header.value == 'gzip'
        for header in package.headers_list
    )
    return gzip.decompress(payload) if compressed else payload


def generateSignature(wss, script_file=None):
    """
    生成WebSocket签名
    :param wss: WebSocket URL
    :param script_file: sign.js文件路径（默认使用crawler/js/sign.js）
    """
    if script_file is None:
        script_file = os.path.join(JS_DIR, 'sign.js')

    params = ("live_id,aid,version_code,webcast_sdk_version,"
              "room_id,sub_room_id,sub_channel_id,did_rule,"
              "user_unique_id,device_platform,device_type,ac,"
              "identity").split(',')
    wss_params = urllib.parse.urlparse(wss).query.split('&')
    wss_maps = {i.split('=')[0]: i.split("=")[-1] for i in wss_params}
    tpl_params = [f"{i}={wss_maps.get(i, '')}" for i in params]
    param = ','.join(tpl_params)
    md5 = hashlib.md5()
    md5.update(param.encode())
    md5_param = md5.hexdigest()

    try:
        signature = _call_js(script_file, "get_sign", md5_param)
        if not signature:
            raise ValueError("WebSocket 签名生成结果为空")
        return signature
    except Exception as e:
        log = get_logger("signature")
        log.error(f"签名生成失败: {e}")
        raise


def generateMsToken(length=182):
    """
    产生请求头部cookie中的msToken字段，其实为随机的107位字符
    :param length:字符位数
    :return:msToken
    """
    random_str = ''
    base_str = string.ascii_letters + string.digits + '-_'
    _len = len(base_str) - 1
    for _ in range(length):
        random_str += base_str[random.randint(0, _len)]
    return random_str


class DouyinLiveWebFetcher:
    """抖音直播数据爬虫核心类"""
    BUSINESS_MESSAGE_METHODS = {
        'WebcastChatMessage',
        'WebcastGiftMessage',
        'WebcastBindingGiftMessage',
        'WebcastLikeMessage',
        'WebcastMemberMessage',
        'WebcastSocialMessage',
        'WebcastFansclubMessage',
        'WebcastEmojiChatMessage',
    }

    def __init__(self, live_id, abogus_file=None, proxy_enabled=None, proxy_url=None, douyin_cookie=None):
        """
        直播间弹幕抓取对象
        :param live_id: 直播间的直播id，打开直播间web首页的链接如：https://live.douyin.com/261378947940，
                        其中的261378947940即是live_id
        :param abogus_file: a_bogus.js文件路径（默认使用crawler/js/a_bogus.js）
        :param proxy_enabled: 是否启用代理（None则从配置文件读取）
        :param proxy_url: 代理URL（None则从配置文件读取）
        :param douyin_cookie: 已登录 live.douyin.com 后取得的 Cookie（None则从配置文件读取）
        """
        # 默认JS文件路径
        if abogus_file is None:
            abogus_file = os.path.join(JS_DIR, 'a_bogus.js')

        self.abogus_file = abogus_file
        self.__ttwid = None
        self.__room_id = None
        self._cached_anchor_name = None  # 缓存主播名字
        self._cached_anchor_id = None    # 缓存主播ID
        self.session = requests.Session()
        self.request_timeout = (5, 15)
        self.status_error_message = None
        self.ws = None
        self._ws_state_lock = threading.Lock()
        self._ws_watchdog_stop = threading.Event()
        self._ws_watchdog_thread = None
        self._ws_connect_started_at = None
        self._ws_connected_at = None
        self._ws_last_data_at = None
        self._ws_last_business_at = None
        self.live_id = live_id
        self.host = "https://www.douyin.com/"
        self.live_url = "https://live.douyin.com/"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
        self.douyin_cookie = (douyin_cookie if douyin_cookie is not None else config.DOUYIN_COOKIE).strip()
        self._configured_cookies = self._parse_cookie(self.douyin_cookie)
        self.__ttwid = self._configured_cookies.get('ttwid')
        self.headers = {
            'User-Agent': self.user_agent
        }
        if self._configured_cookies:
            self.session.cookies.update(self._configured_cookies)
            self.headers['Cookie'] = self._build_cookie_header()

        # 代理配置
        self.proxy_enabled = proxy_enabled if proxy_enabled is not None else config.PROXY_ENABLED
        self.proxy_url = proxy_url if proxy_url is not None else config.get_proxy_url()
        self.proxies = config.get_proxy_config()

        if self.proxy_enabled and self.proxies:
            self.session.proxies.update(self.proxies)
            self.log = get_logger("liveMan", live_id)
            self.log.info(f"已启用代理: {self.proxy_url}")
        else:
            self.log = get_logger("liveMan", live_id)

    @staticmethod
    def _parse_cookie(cookie: str) -> dict:
        """
        将浏览器复制的 Cookie 字符串解析为字典。
        """
        cookies = {}
        if not cookie:
            return cookies
        for item in cookie.split(';'):
            if '=' not in item:
                continue
            key, value = item.split('=', 1)
            key = key.strip()
            if key:
                cookies[key] = value.strip()
        return cookies

    def _build_cookie_header(self, extra_cookies: dict = None) -> str:
        """
        合并登录 Cookie 和本次请求需要的临时 Cookie。
        """
        cookies = self._configured_cookies.copy()
        if extra_cookies:
            for key, value in extra_cookies.items():
                if value is not None and value != '':
                    cookies[key] = str(value)
        return '; '.join(f'{key}={value}' for key, value in cookies.items())

    def _headers_with_cookie(self, extra_cookies: dict = None) -> dict:
        headers = self.headers.copy()
        cookie = self._build_cookie_header(extra_cookies)
        if cookie:
            headers['Cookie'] = cookie
        return headers

    def update_douyin_cookie(self, douyin_cookie: str):
        """
        更新运行中的 Cookie 配置，供后续 HTTP 请求和下一次 WebSocket 重连使用。
        """
        self.douyin_cookie = (douyin_cookie or '').strip()
        self._configured_cookies = self._parse_cookie(self.douyin_cookie)
        self.__ttwid = self._configured_cookies.get('ttwid')
        self.session.cookies.clear()
        if self._configured_cookies:
            self.session.cookies.update(self._configured_cookies)
            self.headers['Cookie'] = self._build_cookie_header()
        else:
            self.headers.pop('Cookie', None)

    def start(self):
        self._connectWebSocket()

    def stop(self):
        self._stop_websocket_watchdog()
        ws = self.ws
        if ws is None:
            return
        sock = getattr(ws, 'sock', None)
        try:
            if sock is not None:
                sock.close()
        except Exception as e:
            self.log.debug(f"关闭底层 socket 失败（已忽略）: {e}")
        finally:
            ws.close()

    def close(self):
        """释放 HTTP Session 及其连接池。"""
        self.session.close()

    def update_log_context(self, anchor_name: str = None):
        """
        更新日志上下文，使用主播名字
        :param anchor_name: 主播名字
        """
        from utils.logger import get_logger
        room_display = f"{anchor_name}({self.live_id})" if anchor_name else f"({self.live_id})"
        self.log = get_logger("liveMan", room_display)

    def _request_failure_message(self, err):
        if isinstance(err, requests.exceptions.ProxyError):
            return "代理连接异常，等待重试..."
        if isinstance(err, requests.exceptions.Timeout):
            return "网络请求超时，等待重试..."
        if isinstance(err, requests.exceptions.ConnectionError):
            return "网络连接异常，等待重试..."
        if isinstance(err, requests.exceptions.HTTPError):
            response = getattr(err, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in (401, 403, 429):
                return "抖音请求受限，等待重试..."
            return "抖音接口请求失败，等待重试..."
        return "直播间状态检测失败，等待重试..."

    @property
    def ttwid(self):
        """
        产生请求头部cookie中的ttwid字段，访问抖音网页版直播间首页可以获取到响应cookie中的ttwid
        :return: ttwid
        """
        if self.__ttwid:
            return self.__ttwid
        headers = {
            "User-Agent": self.user_agent,
        }
        try:
            response = self.session.get(self.live_url, headers=headers, timeout=self.request_timeout)
            response.raise_for_status()
        except Exception as err:
            print("【X】Request the live url error: ", err)
        else:
            self.__ttwid = response.cookies.get('ttwid')
            return self.__ttwid

    @property
    def room_id(self):
        """
        根据直播间的地址获取到真正的直播间roomId，有时会有错误，可以重试请求解决
        :return:room_id
        """
        if self.__room_id:
            return self.__room_id
        url = self.live_url + self.live_id
        headers = self._headers_with_cookie({
            "ttwid": self.ttwid,
            "msToken": generateMsToken(),
            "__ac_nonce": "0123407cc00a9e438deb4",
        })
        try:
            response = self.session.get(url, headers=headers, timeout=self.request_timeout)
            response.raise_for_status()
        except Exception as err:
            self.log.error(f"Request the live room url error: {err}")
            self.status_error_message = self._request_failure_message(err)
            # 设置缓存为 None，避免重复请求
            self.__room_id = None
            return None
        else:
            match = re.search(r'roomId\\":\\"(\d+)\\"', response.text)
            if match is None or len(match.groups()) < 1:
                self.log.warning("No match found for roomId, 直播间可能已结束")
                # 设置缓存为 None，避免重复请求
                self.status_error_message = "无法获取直播间信息，等待重试..."
                self.__room_id = None
                return None

            self.__room_id = match.group(1)

            return self.__room_id

    @property
    def anchor_info(self):
        """
        从抖音 API 获取主播信息
        :return: dict with anchor_name and anchor_id
        """
        # 优先返回缓存值
        if self._cached_anchor_name or self._cached_anchor_id:
            return {
                'anchor_name': self._cached_anchor_name,
                'anchor_id': self._cached_anchor_id
            }

        # 先尝试获取 room_id
        if not self.room_id:
            self.log.warning("无法获取 room_id，使用备用方法获取主播信息")
            return self._get_anchor_info_from_html()

        try:
            msToken = generateMsToken()
            nonce = self.get_ac_nonce()
            signature = self.get_ac_signature(nonce)
            url = ('https://live.douyin.com/webcast/room/web/enter/?aid=6383'
                   '&app_name=douyin_web&live_id=1&device_platform=web&language=zh-CN&enter_from=page_refresh'
                   '&cookie_enabled=true&screen_width=5120&screen_height=1440&browser_language=zh-CN&browser_platform=Win32'
                   '&browser_name=Edge&browser_version=140.0.0.0'
                   f'&web_rid={self.live_id}'
                   f'&room_id_str={self.room_id}'
                   '&enter_source=&is_need_double_stream=false&insert_task_id=&live_reason=&msToken=' + msToken)
            query = parse_url(url).query
            params = {i[0]: i[1] for i in [j.split('=') for j in query.split('&')]}
            a_bogus = self.get_a_bogus(params)
            url += f"&a_bogus={a_bogus}"
            headers = self._headers_with_cookie({
                'ttwid': self.ttwid,
                '__ac_nonce': nonce,
                '__ac_signature': signature,
            })
            headers.update({
                'Referer': f'https://live.douyin.com/{self.live_id}',
            })
            resp = self.session.get(url, headers=headers, timeout=self.request_timeout)

            if not resp.text or len(resp.text) == 0:
                self.log.warning("API 返回空响应，使用备用方法获取主播信息")
                return self._get_anchor_info_from_html()

            data = resp.json().get('data')
            if data and data.get('user'):
                user = data.get('user')
                anchor_name = user.get('nickname')
                anchor_id = user.get('id_str')
                # 缓存主播信息
                if not self._cached_anchor_name:
                    self._cached_anchor_name = anchor_name
                if not self._cached_anchor_id:
                    self._cached_anchor_id = anchor_id
                self.log.info(f"从 API 获取主播信息: 【{anchor_name}】[{anchor_id}]")
                return {'anchor_name': anchor_name, 'anchor_id': anchor_id}
            else:
                self.log.warning("API 未返回用户信息，使用备用方法获取主播信息")
                return self._get_anchor_info_from_html()

        except Exception as e:
            self.log.warning(f"从 API 获取主播信息失败: {e}，使用备用方法")
            return self._get_anchor_info_from_html()

    def _get_anchor_info_from_html(self):
        """
        备用方法：从直播间 HTML 中提取主播信息（使用正则表达式）
        :return: dict with anchor_name and anchor_id
        """
        url = self.live_url + self.live_id
        headers = self._headers_with_cookie({
            "ttwid": self.ttwid,
            "msToken": generateMsToken(),
            "__ac_nonce": "0123407cc00a9e438deb4",
        })
        try:
            response = self.session.get(url, headers=headers, timeout=self.request_timeout)
            response.raise_for_status()
        except Exception as err:
            self.log.error(f"从 HTML 获取主播信息失败: {err}")
            return {'anchor_name': None, 'anchor_id': None}
        else:
            anchor_name = None
            anchor_id = None
            decoder = json.JSONDecoder()
            # 页面可能嵌套序列化 JSON；仅定位 owner 对象，再解析其直接字段。
            for source in (response.text, response.text.replace(r'\"', '"')):
                for match in re.finditer(r'"owner"\s*:\s*(\{)', source):
                    try:
                        owner, _ = decoder.raw_decode(source, match.start(1))
                    except ValueError:
                        continue
                    anchor_name = owner.get('nickname')
                    owner_id = owner.get('id_str') or owner.get('id')
                    anchor_id = str(owner_id) if owner_id is not None else None
                    if anchor_name or anchor_id:
                        break
                if anchor_name or anchor_id:
                    break

            if anchor_name or anchor_id:
                # 缓存主播信息
                if not self._cached_anchor_name:
                    self._cached_anchor_name = anchor_name
                if not self._cached_anchor_id:
                    self._cached_anchor_id = anchor_id
                self.log.info(f"从 HTML 获取主播信息: 【{anchor_name}】[{anchor_id}]")
            return {'anchor_name': anchor_name, 'anchor_id': anchor_id}

    def get_ac_nonce(self):
        """
        获取 __ac_nonce
        """
        resp_cookies = self.session.get(self.host, headers=self.headers, timeout=self.request_timeout).cookies
        return resp_cookies.get("__ac_nonce")

    def get_ac_signature(self, __ac_nonce: str = None) -> str:
        """
        获取 __ac_signature
        """
        __ac_signature = get__ac_signature(self.host[8:], __ac_nonce, self.user_agent)
        self.session.cookies.set("__ac_signature", __ac_signature)
        return __ac_signature

    def get_a_bogus(self, url_params: dict):
        """
        获取 a_bogus
        """
        url = urllib.parse.urlencode(url_params)
        return _call_js(self.abogus_file, "get_ab", url, self.user_agent)

    def get_room_status(self):
        """
        获取直播间开播状态:
        room_status: 2 直播已结束
        room_status: 0 直播进行中
        :return: True 表示正在直播, False 表示未开播, None 表示状态请求失败
        """
        self.status_error_message = None
        try:
            _throttle_status_probe()
            # 检查 room_id 是否可用
            if not self.room_id:
                self.log.warning("无法获取 roomId，直播间可能已结束")
                if not self.status_error_message:
                    self.status_error_message = "无法获取直播间信息，等待重试..."
                return None

            # 添加防风控排队读取延迟
            if config.ANTI_DETECTION_ENABLED and config.ANTI_DETECTION_QUEUE_READ_DELAY > 0:
                time.sleep(config.ANTI_DETECTION_QUEUE_READ_DELAY)

            msToken = generateMsToken()
            nonce = self.get_ac_nonce()
            signature = self.get_ac_signature(nonce)
            url = ('https://live.douyin.com/webcast/room/web/enter/?aid=6383'
                   '&app_name=douyin_web&live_id=1&device_platform=web&language=zh-CN&enter_from=page_refresh'
                   '&cookie_enabled=true&screen_width=5120&screen_height=1440&browser_language=zh-CN&browser_platform=Win32'
                   '&browser_name=Edge&browser_version=140.0.0.0'
                   f'&web_rid={self.live_id}'
                   f'&room_id_str={self.room_id}'
                   '&enter_source=&is_need_double_stream=false&insert_task_id=&live_reason=&msToken=' + msToken)
            query = parse_url(url).query
            params = {i[0]: i[1] for i in [j.split('=') for j in query.split('&')]}
            a_bogus = self.get_a_bogus(params)
            url += f"&a_bogus={a_bogus}"
            headers = self._headers_with_cookie({
                'ttwid': self.ttwid,
                '__ac_nonce': nonce,
                '__ac_signature': signature,
            })
            headers.update({
                'Referer': f'https://live.douyin.com/{self.live_id}',
            })
            resp = self.session.get(url, headers=headers, timeout=self.request_timeout)
            resp.raise_for_status()
            self.status_error_message = "直播间接口返回异常，等待重试..."

            # 检查响应内容是否为空
            if not resp.text or len(resp.text) == 0:
                self.log.debug("无法获取直播间状态（API返回空响应）")
                return None

            # 解析 JSON 响应
            try:
                json_data = resp.json()
            except ValueError:
                self.log.debug("无法解析直播间状态（响应不是有效的JSON）")
                return None

            # 检查 JSON 数据结构
            if not json_data or not isinstance(json_data, dict):
                self.log.debug("直播间状态响应格式异常（空数据或非字典）")
                return None

            data = json_data.get('data')
            if not data or not isinstance(data, dict):
                self.log.debug("直播间状态响应缺少 data 字段")
                return None

            room_status = data.get('room_status')
            user = data.get('user')

            # 检查 room_status
            if not isinstance(room_status, int) or isinstance(room_status, bool):
                self.log.debug("直播间状态响应缺少 room_status 字段")
                return None

            self.status_error_message = None

            # 处理主播信息
            if user and isinstance(user, dict):
                user_id = user.get('id_str')
                nickname = user.get('nickname')
                # 缓存主播信息
                if nickname and not self._cached_anchor_name:
                    self._cached_anchor_name = nickname
                if user_id and not self._cached_anchor_id:
                    self._cached_anchor_id = user_id

                is_live = room_status == 0
                status_text = '正在直播' if is_live else '已结束'
                self.log.debug(f"【{nickname or '未知主播'}】[{user_id or '未知ID'}]直播间：{status_text}")
                return is_live
            else:
                # 没有 user 信息，但可以根据 room_status 判断
                is_live = room_status == 0
                self.log.debug(f"直播间状态：{'正在直播' if is_live else '已结束'}")
                return is_live

        except Exception as e:
            if isinstance(e, requests.exceptions.RequestException):
                self.status_error_message = self._request_failure_message(e)
                self.log.debug(f"获取直播间状态时出错: {e}")
                return None
            if "NoneType" in str(e) and "not iterable" in str(e):
                self.status_error_message = "直播间接口返回异常，等待重试..."
                self.log.debug(f"获取直播间状态时出错: {e}")
                return None
            self.status_error_message = "直播间状态检测失败，等待重试..."
            self.log.debug(f"获取直播间状态时出错: {e}")
            return None

    def _reset_websocket_watchdog_state(self, started_at=None):
        now = time.monotonic() if started_at is None else started_at
        with self._ws_state_lock:
            self._ws_connect_started_at = now
            self._ws_connected_at = None
            self._ws_last_data_at = None
            self._ws_last_business_at = None

    def record_websocket_open(self, now=None):
        now = time.monotonic() if now is None else now
        with self._ws_state_lock:
            self._ws_connected_at = now
            self._ws_last_data_at = None
            self._ws_last_business_at = now

    def record_websocket_data(self, now=None):
        now = time.monotonic() if now is None else now
        with self._ws_state_lock:
            self._ws_last_data_at = now

    def record_websocket_method(self, method, now=None):
        now = time.monotonic() if now is None else now
        with self._ws_state_lock:
            self._ws_last_data_at = now
            if method in self.BUSINESS_MESSAGE_METHODS:
                self._ws_last_business_at = now

    def _start_websocket_watchdog(self):
        if self._ws_watchdog_thread and self._ws_watchdog_thread.is_alive():
            return
        self._ws_watchdog_stop.clear()
        self._ws_watchdog_thread = threading.Thread(
            target=self._websocket_watchdog_loop,
            daemon=True,
            name=f"ws-watchdog-{self.live_id}"
        )
        self._ws_watchdog_thread.start()

    def _stop_websocket_watchdog(self):
        self._ws_watchdog_stop.set()

    def _websocket_watchdog_loop(self):
        while not self._ws_watchdog_stop.wait(config.WS_WATCHDOG_INTERVAL):
            if self._check_websocket_watchdog():
                break

    def _check_websocket_watchdog(self, now=None):
        now = time.monotonic() if now is None else now
        reason = self._get_websocket_watchdog_close_reason(now)
        if not reason:
            return False
        self._close_websocket_for_watchdog(reason)
        return True

    def _get_websocket_watchdog_close_reason(self, now):
        with self._ws_state_lock:
            connect_started_at = self._ws_connect_started_at
            connected_at = self._ws_connected_at
            last_data_at = self._ws_last_data_at
            last_business_at = self._ws_last_business_at

        if connect_started_at and not connected_at:
            if now - connect_started_at >= config.WS_CONNECT_TIMEOUT:
                return f"连接建立超时 {config.WS_CONNECT_TIMEOUT}s"

        if connected_at:
            data_reference = last_data_at or connected_at
            if now - data_reference >= config.WS_DATA_SILENCE_TIMEOUT:
                return f"数据静默超时 {config.WS_DATA_SILENCE_TIMEOUT}s"

            if (
                config.WS_BUSINESS_WATCHDOG_ENABLED
                and last_data_at
                and last_data_at > connected_at
            ):
                business_reference = last_business_at or connected_at
                if now - business_reference >= config.WS_BUSINESS_SILENCE_TIMEOUT:
                    return f"业务消息静默超时 {config.WS_BUSINESS_SILENCE_TIMEOUT}s"

        return None

    def _close_websocket_for_watchdog(self, reason):
        self.log.warning(f"WebSocket看门狗触发: {reason}，关闭连接以重连")
        ws = self.ws
        if not ws:
            return

        sock = getattr(ws, 'sock', None)
        if sock:
            try:
                sock.close()
            except Exception as e:
                self.log.debug(f"关闭底层 socket 失败（已忽略）: {e}")

        try:
            ws.close()
        except Exception as e:
            self.log.debug(f"关闭 WebSocket 失败（已忽略）: {e}")

    def _connectWebSocket(self):
        """
        连接抖音直播间websocket服务器，请求直播间数据
        """
        wss = ("wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
               "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
               "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
               "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
               "&browser_name=Mozilla"
               "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
               "%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
               "&browser_online=true&tz_name=Asia/Shanghai"
               "&cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
               f"&internal_ext=internal_src:dim|wss_push_room_id:{self.room_id}|wss_push_did:7319483754668557238"
               f"|first_req_ms:1721106114541|fetch_time:1721106114633|seq:1|wss_info:0-1721106114633-0-0|"
               f"wrds_v:7392094459690748497"
               f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
               f"&user_unique_id=7319483754668557238&im_path=/webcast/im/fetch/&identity=audience"
               f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={self.room_id}&heartbeatDuration=0")

        signature = generateSignature(wss)
        wss += f"&signature={signature}"

        headers = {
            "cookie": self._build_cookie_header({"ttwid": self.ttwid}),
            'user-agent': self.user_agent,
        }
        self.ws = websocket.WebSocketApp(wss,
                                         header=headers,
                                         on_open=self._wsOnOpen,
                                         on_message=self._wsOnMessage,
                                         on_error=self._wsOnError,
                                         on_close=self._wsOnClose)
        self._reset_websocket_watchdog_state()
        self._start_websocket_watchdog()
        try:
            # 代理配置
            if self.proxy_enabled and self.proxy_url:
                proxy_host = config.PROXY_HOST
                proxy_port = config.PROXY_PORT
                proxy_type = config.PROXY_TYPE  # http, socks4, or socks5
                self.log.info(f"WebSocket 使用代理: {proxy_type}://{proxy_host}:{proxy_port}")
                self.ws.run_forever(
                    http_proxy_host=proxy_host,
                    http_proxy_port=proxy_port,
                    proxy_type=proxy_type
                )
            else:
                self.ws.run_forever()
        except Exception:
            self.stop()
            raise
        finally:
            self._stop_websocket_watchdog()

    def _sendHeartbeat(self):
        """
        发送心跳包
        """
        while not self._ws_watchdog_stop.is_set():
            try:
                heartbeat = PushFrame(payload_type='hb').SerializeToString()
                self.ws.send(heartbeat, websocket.ABNF.OPCODE_PING)
                self.log.debug("发送心跳包")
            except Exception as e:
                self.log.error(f"心跳包检测错误: {e}")
                break
            else:
                time.sleep(config.WS_HEARTBEAT_INTERVAL)

    def _wsOnOpen(self, ws):
        """
        连接建立成功
        """
        self.record_websocket_open()
        self.log.success("WebSocket连接成功")
        threading.Thread(target=self._sendHeartbeat, daemon=True).start()

    def _wsOnMessage(self, ws, message):
        """
        接收到数据
        :param ws: websocket实例
        :param message: 数据
        """
        self.record_websocket_data()

        # 根据proto结构体解析对象
        package = PushFrame().parse(message)
        payload = decompress_push_frame_payload(package)
        if not payload:
            return
        response = Response().parse(payload)

        # 返回直播间服务器链接存活确认消息，便于持续获取数据
        if response.need_ack:
            ack = PushFrame(log_id=package.log_id,
                            payload_type='ack',
                            payload=response.internal_ext.encode('utf-8')
                            ).SerializeToString()
            ws.send(ack, websocket.ABNF.OPCODE_BINARY)

        # 根据消息类别解析消息体
        if response.messages_list:
            for msg in response.messages_list:
                method = msg.method
                self.record_websocket_method(method)
                try:
                    {
                        'WebcastChatMessage': self._parseChatMsg,  # 聊天消息
                        'WebcastGiftMessage': self._parseGiftMsg,  # 礼物消息
                        'WebcastBindingGiftMessage': self._parseBindingGiftMsg,
                        'WebcastLikeMessage': self._parseLikeMsg,  # 点赞消息
                        'WebcastMemberMessage': self._parseMemberMsg,  # 进入直播间消息
                        'WebcastSocialMessage': self._parseSocialMsg,  # 关注消息
                        'WebcastRoomUserSeqMessage': self._parseRoomUserSeqMsg,  # 直播间统计
                        'WebcastFansclubMessage': self._parseFansclubMsg,  # 粉丝团消息
                        'WebcastControlMessage': self._parseControlMsg,  # 直播间状态消息
                        'WebcastEmojiChatMessage': self._parseEmojiChatMsg,  # 聊天表情包消息
                        'WebcastRoomStatsMessage': self._parseRoomStatsMsg,  # 直播间统计信息
                        'WebcastRoomMessage': self._parseRoomMsg,  # 直播间信息
                        'WebcastRoomRankMessage': self._parseRankMsg,  # 直播间排行榜信息
                        'WebcastRoomStreamAdaptationMessage': self._parseRoomStreamAdaptationMsg,  # 直播间流配置
                    }.get(method)(msg.payload)
                except Exception:
                    pass

    def _wsOnError(self, ws, error):
        self.log.error(f"WebSocket错误: {error}")

    def _wsOnClose(self, ws, *args):
        self.get_room_status()
        self.log.warning("WebSocket连接已关闭")

    def _parseChatMsg(self, payload):
        """聊天消息"""
        message = ChatMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        content = message.content
        self.log.debug(f"【聊天】[{user_id}]{user_name}: {content}")

    def _parseGiftMsg(self, payload):
        """礼物消息"""
        message = GiftMessage().parse(payload)
        user_name = message.user.nick_name
        gift_name = message.gift.name
        gift_cnt = message.combo_count
        self.log.debug(f"【礼物】{user_name} 送出了 {gift_name}x{gift_cnt}")

    def _parseBindingGiftMsg(self, payload):
        """安全解析绑定礼物，供协议日志和业务活跃状态跟踪使用。"""
        try:
            message = BindingGiftMessage().parse(payload)
            self.log.debug(
                f"【绑定礼物】{message.msg.user.nick_name} 送出了 "
                f"{message.msg.gift.name}x{message.msg.group_count or message.msg.repeat_count or 1}"
            )
        except Exception:
            self.log.debug("绑定礼物消息解析失败")

    def _parseLikeMsg(self, payload):
        '''点赞消息'''
        message = LikeMessage().parse(payload)
        user_name = message.user.nick_name
        count = message.count
        self.log.debug(f"【点赞】{user_name} 点了{count}个赞")

    def _parseMemberMsg(self, payload):
        '''进入直播间消息'''
        message = MemberMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        gender = ["女", "男"][message.user.gender]
        self.log.debug(f"【进场】[{user_id}][{gender}]{user_name} 进入了直播间")

    def _parseSocialMsg(self, payload):
        '''关注消息'''
        message = SocialMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        self.log.debug(f"【关注】[{user_id}]{user_name} 关注了主播")

    def _parseRoomUserSeqMsg(self, payload):
        '''直播间统计'''
        message = RoomUserSeqMessage().parse(payload)
        current = message.total
        total = message.total_pv_for_anchor
        self.log.debug(f"【统计】当前观看人数: {current}, 累计观看人数: {total}")

    def _parseFansclubMsg(self, payload):
        '''粉丝团消息'''
        message = FansclubMessage().parse(payload)
        content = message.content
        self.log.debug(f"【粉丝团】{content}")

    def _parseEmojiChatMsg(self, payload):
        '''聊天表情包消息'''
        message = EmojiChatMessage().parse(payload)
        emoji_id = message.emoji_id
        user = message.user
        common = message.common
        default_content = message.default_content
        self.log.debug(f"【表情包】emoji_id={emoji_id}, default_content={default_content}")

    def _parseRoomMsg(self, payload):
        message = RoomMessage().parse(payload)
        common = message.common
        room_id = common.room_id
        self.log.debug(f"【直播间】直播间id:{room_id}")

    def _parseRoomStatsMsg(self, payload):
        message = RoomStatsMessage().parse(payload)
        display_long = message.display_long
        self.log.debug(f"【直播统计】{display_long}")

    def _parseRankMsg(self, payload):
        message = RoomRankMessage().parse(payload)
        ranks_list = message.ranks_list
        self.log.debug(f"【排行榜】{ranks_list}")

    def _parseControlMsg(self, payload):
        '''直播间状态消息'''
        message = ControlMessage().parse(payload)

        if message.status == 3:
            self.log.warning("直播间已结束")
            self.stop()

    def _parseRoomStreamAdaptationMsg(self, payload):
        message = RoomStreamAdaptationMessage().parse(payload)
        adaptationType = message.adaptation_type
        self.log.debug(f'直播间adaptation: {adaptationType}')
