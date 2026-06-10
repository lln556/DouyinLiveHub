# Cookie 测活（健康检测）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 系统能自动发现抖音 Cookie 失活（礼物数据静默缺失的根因），在前端醒目提示并写入系统事件。

**Architecture:** 新增 `services/cookie_health.py` 作为健康状态唯一权威（probe 纯函数 + CookieHealthService 状态机）。被动信号（有弹幕但长时间无礼物）和低频定时探测由一个 5 分钟 tick 的 APScheduler 任务驱动；手动测活和更新 Cookie 后自动测活走同步 API。状态切换写 `system_events`（live_id=None 全局事件）。

**Tech Stack:** Flask + APScheduler + requests + Vue.js（前端轮询）+ pytest（unit/integration/e2e 走现有 harness）

**对 spec 的两处实现调整**（spec: `docs/superpowers/specs/2026-06-10-cookie-health-design.md`，在 Task 8 同步修订）：

1. **Socket.IO 广播 → 前端轮询**：首页 `index.js` 本来就没有 Socket.IO 连接（纯 fetch + 5 秒轮询），为 cookie 状态引入 Socket.IO 反而增加依赖。改为把 `loadCookieConfig()` 加入现有 5 秒轮询。
2. **调度实现具体化**：spec 中"定时探测 30 分钟 + 顺带评估被动信号"落地为单个 5 分钟 `tick()` 任务：每次 tick 评估被动信号（命中且距上次探测 ≥15 分钟则探测）；距上次探测 ≥ `COOKIE_HEALTH_CHECK_INTERVAL` 则定时探测。单任务、逻辑集中、可测。

**全局约定**：

- 测试一律通过 `./scripts/test.sh unit|integration|e2e` 运行（内部用 uv），不要直接调 pytest/python。
- 测试 harness 关键点：`tests/conftest.py` 顶层先 load `tests/.env.test` 锁死测试库；`clean_db` autouse 每个测试 truncate 业务表；`api_client` 是 in-process Flask test_client 适配器（`.status`/`.json()`）。
- 时区时间用 `models.database.get_china_now()`；内存时间戳（被动信号）用 `time.time()`。
- 提交信息遵循 Conventional Commits，中文 description。

---

### Task 1: 配置项 + probe 纯函数（TDD）

**Files:**
- Modify: `config.py:100`（调度器配置段之后、WebSocket 配置段之前）
- Create: `services/cookie_health.py`
- Test: `tests/test_cookie_health_probe.py`
- Create: `scripts/check_cookie.sh`

- [ ] **Step 1: 在 config.py 添加配置项**

在 `config.py` 第 100 行（`SCHEDULER_CLEANUPOldData_INTERVAL` 行）之后插入：

```python

# Cookie 健康检测配置
COOKIE_HEALTH_CHECK_INTERVAL = int(os.getenv('COOKIE_HEALTH_CHECK_INTERVAL', '1800'))  # 定时探测间隔(秒)，0=关闭定时探测
COOKIE_HEALTH_GIFT_SILENCE = int(os.getenv('COOKIE_HEALTH_GIFT_SILENCE', '1800'))  # 被动信号: 无礼物多久算可疑(秒)
COOKIE_HEALTH_CHAT_ACTIVE = int(os.getenv('COOKIE_HEALTH_CHAT_ACTIVE', '600'))  # 被动信号: 多久内有弹幕算活跃(秒)
```

- [ ] **Step 2: 写 probe 函数的失败测试**

创建 `tests/test_cookie_health_probe.py`：

```python
"""probe_douyin_cookie 纯函数单测：alive / dead / inconclusive 三种判定。"""
from unittest.mock import patch

import requests

from services.cookie_health import CookieProbeResult, probe_douyin_cookie


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def _probe_with(response=None, exc=None):
    with patch("services.cookie_health.requests.get") as mock_get:
        if exc is not None:
            mock_get.side_effect = exc
        else:
            mock_get.return_value = response
        return probe_douyin_cookie("sessionid=abc; ttwid=xyz")


def test_logged_in_user_returns_alive():
    resp = _FakeResponse(payload={"status_code": 0, "data": {"id_str": "12345"}})
    result = _probe_with(response=resp)
    assert result.outcome == "alive"


def test_anonymous_user_returns_dead():
    """接口正常返回但用户 id 为 0/空 → 明确未登录。"""
    resp = _FakeResponse(payload={"status_code": 0, "data": {"id_str": "0"}})
    assert _probe_with(response=resp).outcome == "dead"

    resp = _FakeResponse(payload={"status_code": 0, "data": {}})
    assert _probe_with(response=resp).outcome == "dead"


def test_error_status_code_returns_dead():
    resp = _FakeResponse(payload={"status_code": 8, "data": None})
    assert _probe_with(response=resp).outcome == "dead"


def test_network_error_returns_inconclusive():
    result = _probe_with(exc=requests.exceptions.ConnectionError("boom"))
    assert result.outcome == "inconclusive"


def test_http_5xx_returns_inconclusive():
    resp = _FakeResponse(status_code=502)
    assert _probe_with(response=resp).outcome == "inconclusive"


def test_non_json_returns_inconclusive():
    resp = _FakeResponse(raise_json=True)
    assert _probe_with(response=resp).outcome == "inconclusive"
```

- [ ] **Step 3: 运行测试确认失败**

Run: `./scripts/test.sh unit tests/test_cookie_health_probe.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'services.cookie_health'`

- [ ] **Step 4: 创建 services/cookie_health.py（probe 部分）**

```python
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

# 登录态自检接口（判定字段以 scripts/check_cookie.sh 实测为准，见 Task 2）
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
```

（`threading`、`get_china_now`、`logger` 现在还未用到，Task 3 的状态机会用；保留 import。）

- [ ] **Step 5: 运行测试确认通过**

Run: `./scripts/test.sh unit tests/test_cookie_health_probe.py -v`
Expected: 6 个测试全部 PASS

- [ ] **Step 6: 创建 scripts/check_cookie.sh**

```bash
#!/bin/bash
# Cookie 测活手动验证：打印探测接口原始响应与判定结果
#
# 用法:
#   ./scripts/check_cookie.sh                          # 用 .env 中的 DOUYIN_COOKIE
#   DOUYIN_COOKIE="ttwid=xxx" ./scripts/check_cookie.sh  # 用环境变量覆盖（dotenv 不覆盖已有环境变量）
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

uv run python -m services.cookie_health
```

然后执行 `chmod +x scripts/check_cookie.sh`。

- [ ] **Step 7: Commit**

```bash
git add config.py services/cookie_health.py tests/test_cookie_health_probe.py scripts/check_cookie.sh
git commit -m "feat(cookie-health): 新增 Cookie 登录态探测函数与手动验证脚本"
```

---

### Task 2: 真实环境实测校准判定字段

probe 的判定字段（`status_code`/`data.id_str`）基于对 `webcast/user/me` 接口的假定，必须实测校准后才能继续。

**Files:**
- Modify（仅当实测不符）: `services/cookie_health.py`、`tests/test_cookie_health_probe.py`

- [ ] **Step 1: 用 .env 中的真实（活）Cookie 实测**

Run: `./scripts/check_cookie.sh`
Expected: 打印 `HTTP 200`、响应 JSON 前 2000 字符、`判定: alive (...)`。
记录响应中表示登录用户的实际字段路径。

- [ ] **Step 2: 用坏 Cookie 实测**

Run: `DOUYIN_COOKIE="ttwid=invalid_for_test" ./scripts/check_cookie.sh`
Expected: `判定: dead (...)`。
若接口对无效 Cookie 返回非 200 或风控页（导致 inconclusive 而非 dead），记录实际行为。

- [ ] **Step 3: 校准判定逻辑（仅当实测与假定不符）**

若实际响应结构与假定不同（例如用户字段是 `data.user.id_str`、未登录时 `status_code` 非 0、或返回特定错误码），同步修改：
1. `services/cookie_health.py` 中 `probe_douyin_cookie` 的判定分支
2. `tests/test_cookie_health_probe.py` 中对应的 mock payload

校准原则不变：**只有"接口正常响应且明确表示无登录用户"才判 dead**，其余一律 inconclusive。

- [ ] **Step 4: 重跑单测确认通过**

Run: `./scripts/test.sh unit tests/test_cookie_health_probe.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit（仅当有修改）**

```bash
git add services/cookie_health.py tests/test_cookie_health_probe.py
git commit -m "fix(cookie-health): 按实测响应校准登录态判定字段"
```

---

### Task 3: CookieHealthService 状态机（TDD）

**Files:**
- Modify: `services/cookie_health.py`（追加 CookieHealthService 类）
- Test: `tests/test_cookie_health_service.py`

- [ ] **Step 1: 写状态机的失败测试**

创建 `tests/test_cookie_health_service.py`：

```python
"""CookieHealthService 状态机单测：防抖、恢复、事件写入。probe 全部 mock。"""
import pytest

import config
from services.cookie_health import CookieHealthService, CookieProbeResult


class StubDataService:
    def __init__(self):
        self.events = []

    def log_system_event(self, live_id, event_type, message=None, data=None, anchor_name=None):
        self.events.append((live_id, event_type, message))


class StubRoomManager:
    def __init__(self):
        self.active_rooms = {}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', 'sessionid=test')
    return CookieHealthService(StubRoomManager(), StubDataService())


def _set_probe(monkeypatch, outcome, detail=''):
    monkeypatch.setattr(
        'services.cookie_health.probe_douyin_cookie',
        lambda cookie, proxies=None: CookieProbeResult(outcome, detail))


def test_initial_status_unknown_when_configured(service):
    assert service.status == 'unknown'


def test_initial_status_unconfigured_without_cookie(monkeypatch):
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', '')
    svc = CookieHealthService(StubRoomManager(), StubDataService())
    assert svc.status == 'unconfigured'


def test_alive_probe_sets_healthy(service, monkeypatch):
    _set_probe(monkeypatch, 'alive')
    snapshot = service.run_probe(trigger='manual')
    assert snapshot['status'] == 'healthy'
    assert snapshot['last_ok_time'] is not None
    assert snapshot['trigger'] == 'manual'


def test_single_dead_is_suspect_not_dead(service, monkeypatch):
    """防抖：单次明确未登录只进 suspect。"""
    _set_probe(monkeypatch, 'dead', '未检测到登录态')
    snapshot = service.run_probe(trigger='scheduled')
    assert snapshot['status'] == 'suspect'
    assert service.data_service.events == []  # 未跨界，不写事件


def test_two_consecutive_dead_marks_dead_and_logs_once(service, monkeypatch):
    _set_probe(monkeypatch, 'dead', '未检测到登录态')
    service.run_probe(trigger='scheduled')
    snapshot = service.run_probe(trigger='scheduled')
    assert snapshot['status'] == 'dead'
    dead_events = [e for e in service.data_service.events if e[1] == 'cookie_dead']
    assert len(dead_events) == 1
    assert dead_events[0][0] is None  # 全局事件 live_id=None
    # 再探测一次仍 dead，不重复写事件
    service.run_probe(trigger='scheduled')
    assert len([e for e in service.data_service.events if e[1] == 'cookie_dead']) == 1


def test_skip_debounce_marks_dead_immediately(service, monkeypatch):
    """手动测活跳过防抖：单次未登录即判 dead。"""
    _set_probe(monkeypatch, 'dead', '未检测到登录态')
    snapshot = service.run_probe(trigger='manual', skip_debounce=True)
    assert snapshot['status'] == 'dead'


def test_inconclusive_keeps_status(service, monkeypatch):
    """网络错误不可作为失活证据：状态不变，只记 last_error。"""
    _set_probe(monkeypatch, 'alive')
    service.run_probe(trigger='scheduled')
    _set_probe(monkeypatch, 'inconclusive', '网络请求失败')
    snapshot = service.run_probe(trigger='scheduled')
    assert snapshot['status'] == 'healthy'
    assert snapshot['last_error'] == '网络请求失败'


def test_recover_from_dead_logs_recovered(service, monkeypatch):
    _set_probe(monkeypatch, 'dead', 'x')
    service.run_probe(trigger='manual', skip_debounce=True)
    _set_probe(monkeypatch, 'alive')
    snapshot = service.run_probe(trigger='cookie_updated')
    assert snapshot['status'] == 'healthy'
    recovered = [e for e in service.data_service.events if e[1] == 'cookie_recovered']
    assert len(recovered) == 1


def test_alive_resets_fail_count(service, monkeypatch):
    """dead→alive→dead：fail_count 被重置，单次 dead 又回到 suspect。"""
    _set_probe(monkeypatch, 'dead', 'x')
    service.run_probe(trigger='scheduled')          # suspect, fail_count=1
    _set_probe(monkeypatch, 'alive')
    service.run_probe(trigger='scheduled')          # healthy, fail_count=0
    _set_probe(monkeypatch, 'dead', 'x')
    snapshot = service.run_probe(trigger='scheduled')
    assert snapshot['status'] == 'suspect'


def test_empty_cookie_sets_unconfigured(service, monkeypatch):
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', '')
    snapshot = service.run_probe(trigger='cookie_updated')
    assert snapshot['status'] == 'unconfigured'
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./scripts/test.sh unit tests/test_cookie_health_service.py -v`
Expected: FAIL —— `ImportError: cannot import name 'CookieHealthService'`

- [ ] **Step 3: 在 services/cookie_health.py 追加 CookieHealthService**

在 `probe_douyin_cookie` 函数之后、`if __name__ == '__main__':` 之前插入：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `./scripts/test.sh unit tests/test_cookie_health_service.py -v`
Expected: 11 个测试全部 PASS

- [ ] **Step 5: 检查文件行数**

Run: `wc -l services/cookie_health.py`
Expected: < 300 行（动态语言硬性指标）。若接近，把 `__main__` 块移到独立脚本再评估。

- [ ] **Step 6: Commit**

```bash
git add services/cookie_health.py tests/test_cookie_health_service.py
git commit -m "feat(cookie-health): 实现健康状态机（防抖、恢复、系统事件）"
```

---

### Task 4: 被动信号 + tick 调度逻辑（TDD）

**Files:**
- Modify: `ws_handlers/handlers.py`（消息时间戳）
- Modify: `services/cookie_health.py`（追加 `_passive_signal_hit` 与 `tick`）
- Test: `tests/test_cookie_health_service.py`（追加测试）

- [ ] **Step 1: 写被动信号与 tick 的失败测试**

在 `tests/test_cookie_health_service.py` 末尾追加：

```python
# ───────── 被动信号 & tick ─────────

import time
from types import SimpleNamespace


def _room_with_fetcher(**kw):
    """构造带 fetcher 时间戳属性的假 MonitoredRoom。"""
    return SimpleNamespace(fetcher=SimpleNamespace(
        ws_open_time=kw.get('ws_open_time'),
        last_chat_time=kw.get('last_chat_time'),
        last_gift_time=kw.get('last_gift_time'),
    ))


def test_passive_signal_hits_when_chat_active_but_no_gift(service):
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 30, last_gift_time=None)
    assert service._passive_signal_hit() is True


def test_passive_signal_vetoed_by_recent_gift(service):
    """任一房间近期收到礼物 → Cookie 显然有效，信号否决。"""
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 30, last_gift_time=None)
    service.room_manager.active_rooms['r2'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 30, last_gift_time=now - 60)
    assert service._passive_signal_hit() is False


def test_passive_signal_ignores_freshly_connected_room(service):
    """连接未满 GIFT_SILENCE 的房间不能作为证据（刚连上没礼物很正常）。"""
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 60, last_chat_time=now - 30, last_gift_time=None)
    assert service._passive_signal_hit() is False


def test_passive_signal_ignores_inactive_room(service):
    """没有近期弹幕的房间（没人看/没开播）不能作为证据。"""
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 3600, last_gift_time=None)
    assert service._passive_signal_hit() is False


def test_tick_passive_hit_triggers_probe(service, monkeypatch):
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 30, last_gift_time=None)
    calls = []
    monkeypatch.setattr(service, 'run_probe', lambda trigger, **kw: calls.append(trigger))
    service.tick()
    assert calls == ['passive']


def test_tick_passive_rate_limited(service, monkeypatch):
    """距上次探测不足 15 分钟时被动信号不再触发，但定时探测条件也未到 → 无探测。"""
    now = time.time()
    service.room_manager.active_rooms['r1'] = _room_with_fetcher(
        ws_open_time=now - 7200, last_chat_time=now - 30, last_gift_time=None)
    service._last_probe_at = now - 60  # 1 分钟前刚探测过
    calls = []
    monkeypatch.setattr(service, 'run_probe', lambda trigger, **kw: calls.append(trigger))
    service.tick()
    assert calls == []


def test_tick_scheduled_probe_when_interval_elapsed(service, monkeypatch):
    service._last_probe_at = time.time() - config.COOKIE_HEALTH_CHECK_INTERVAL - 1
    calls = []
    monkeypatch.setattr(service, 'run_probe', lambda trigger, **kw: calls.append(trigger))
    service.tick()
    assert calls == ['scheduled']


def test_tick_first_run_probes_immediately(service, monkeypatch):
    """启动后首次 tick（_last_probe_at 为 None）立即做定时探测。"""
    calls = []
    monkeypatch.setattr(service, 'run_probe', lambda trigger, **kw: calls.append(trigger))
    service.tick()
    assert calls == ['scheduled']


def test_tick_unconfigured_sets_status_and_skips_probe(service, monkeypatch):
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', '')
    calls = []
    monkeypatch.setattr(service, 'run_probe', lambda trigger, **kw: calls.append(trigger))
    service.tick()
    assert calls == []
    assert service.status == 'unconfigured'
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./scripts/test.sh unit tests/test_cookie_health_service.py -v`
Expected: 新增测试 FAIL —— `AttributeError: 'CookieHealthService' object has no attribute '_passive_signal_hit'`

- [ ] **Step 3: 在 CookieHealthService 追加被动信号与 tick**

在 `_transition` 方法之后追加：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `./scripts/test.sh unit tests/test_cookie_health_service.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 在 handlers.py 记录消息时间戳**

`ws_handlers/handlers.py` 修改三处：

① 文件顶部 import 区（`import threading` 之后）加：

```python
import time
```

② `__init__` 中 `self.anchor_name = None  # 主播名称`（约 103 行）之后加：

```python
        # Cookie 被动测活信号（内存时间戳，不入库）
        self.ws_open_time = None    # WebSocket 连接建立时间
        self.last_chat_time = None  # 最近一条弹幕时间
        self.last_gift_time = None  # 最近一条礼物时间
```

③ 三个方法体的第一行分别加（用 Grep 定位 `def _wsOnOpen`、`def _handle_chat_message`、`def _handle_gift_message`）：

```python
        self.ws_open_time = time.time()      # _wsOnOpen 开头
```

```python
        self.last_chat_time = time.time()    # _handle_chat_message 开头
```

```python
        self.last_gift_time = time.time()    # _handle_gift_message 开头
```

（`_handle_gift_message` 在 trace_id 去重之前记录——任何礼物消息到达本身就证明 Cookie 工作正常。）

- [ ] **Step 6: 跑全量单测确认无回归**

Run: `./scripts/test.sh unit`
Expected: 全部 PASS（含现有 `test_websocket_watchdog.py` 等）

- [ ] **Step 7: Commit**

```bash
git add services/cookie_health.py ws_handlers/handlers.py tests/test_cookie_health_service.py
git commit -m "feat(cookie-health): 实现被动信号检测与 tick 调度逻辑"
```

---

### Task 5: app.py API 集成 + 调度注册（集成测试）

**Files:**
- Modify: `app.py`（实例创建、GET 扩展、POST 后探测、新增 check 路由、注册定时任务）
- Test: `tests/integration/test_cookie_health_api.py`

- [ ] **Step 1: 写 API 集成测试**

创建 `tests/integration/test_cookie_health_api.py`：

```python
"""L1 集成测试：Cookie 测活 API。

probe 网络请求一律 monkeypatch 掉；app.cookie_health_service 是模块级单例，
fixture 负责每个测试前重置其内存状态、测试后恢复 config.DOUYIN_COOKIE。
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def health_service():
    import config
    from app import cookie_health_service

    original_cookie = config.DOUYIN_COOKIE
    cookie_health_service.status = 'unknown' if config.DOUYIN_COOKIE else 'unconfigured'
    cookie_health_service.fail_count = 0
    cookie_health_service.last_check_time = None
    cookie_health_service.last_ok_time = None
    cookie_health_service.last_error = None
    cookie_health_service.last_trigger = None
    cookie_health_service._last_probe_at = None
    yield cookie_health_service
    config.DOUYIN_COOKIE = original_cookie


def _set_probe(monkeypatch, outcome, detail=''):
    from services.cookie_health import CookieProbeResult
    monkeypatch.setattr(
        'services.cookie_health.probe_douyin_cookie',
        lambda cookie, proxies=None: CookieProbeResult(outcome, detail))


def test_get_cookie_config_includes_health(api_client, health_service):
    resp = api_client.get('/api/douyin-cookie')
    assert resp.status == 200
    body = resp.json()
    assert 'health' in body
    assert body['health']['status'] in ('unconfigured', 'unknown', 'healthy', 'suspect', 'dead')


def test_manual_check_dead_marks_dead_and_logs_event(api_client, health_service, monkeypatch, data_service):
    import config
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', 'sessionid=broken')
    _set_probe(monkeypatch, 'dead', '未检测到登录态')

    resp = api_client.post('/api/douyin-cookie/check')
    assert resp.status == 200
    body = resp.json()
    assert body['status'] == 'dead'      # 手动测活跳过防抖
    assert body['trigger'] == 'manual'

    events = data_service.get_system_events(event_type='cookie_dead')
    assert len(events) == 1


def test_manual_check_alive_returns_healthy(api_client, health_service, monkeypatch):
    import config
    monkeypatch.setattr(config, 'DOUYIN_COOKIE', 'sessionid=good')
    _set_probe(monkeypatch, 'alive', '登录用户 id=123')

    resp = api_client.post('/api/douyin-cookie/check')
    assert resp.status == 200
    body = resp.json()
    assert body['status'] == 'healthy'
    assert body['last_ok_time'] is not None


def test_update_cookie_probes_immediately(api_client, health_service, monkeypatch):
    """更新 Cookie 后响应自带探测结果，粘贴完立刻知道有效与否。"""
    import app as app_module
    monkeypatch.setattr(app_module, 'update_env_value', lambda key, value: None)  # 不污染 .env
    _set_probe(monkeypatch, 'alive', '登录用户 id=123')

    resp = api_client.post('/api/douyin-cookie', data={'cookie': 'sessionid=new', 'reconnect_active': False})
    assert resp.status == 200
    body = resp.json()
    assert body['success'] is True
    assert body['health']['status'] == 'healthy'
    assert body['health']['trigger'] == 'cookie_updated'


def test_clear_cookie_sets_unconfigured(api_client, health_service, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, 'update_env_value', lambda key, value: None)

    resp = api_client.post('/api/douyin-cookie', data={'cookie': '', 'reconnect_active': False})
    assert resp.status == 200
    assert resp.json()['health']['status'] == 'unconfigured'
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./scripts/test.sh integration tests/integration/test_cookie_health_api.py -v`
Expected: FAIL —— `ImportError: cannot import name 'cookie_health_service' from 'app'`

- [ ] **Step 3: 修改 app.py**

① import 区（`from services.scheduler_service import SchedulerService` 之后）加：

```python
from services.cookie_health import CookieHealthService, COOKIE_HEALTH_TICK_INTERVAL
```

② `scheduler_service = SchedulerService(room_manager, data_service)`（78 行）之后加：

```python
# 初始化 Cookie 健康检测服务
cookie_health_service = CookieHealthService(room_manager, data_service)
```

③ `get_douyin_cookie_config`（257-264 行）的返回值改为：

```python
@app.route('/api/douyin-cookie', methods=['GET'])
def get_douyin_cookie_config():
    """获取 Cookie 配置状态与健康状态，不返回 Cookie 明文。"""
    cookie = config.DOUYIN_COOKIE or ''
    return jsonify({
        'configured': bool(cookie),
        'length': len(cookie),
        'health': cookie_health_service.snapshot()
    })
```

④ `update_douyin_cookie_config`（267-298 行）：在 `updated_rooms = room_manager.update_douyin_cookie(...)` 之后、`logger.info(...)` 之前加一行探测，并把结果加入返回值：

```python
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
```

⑤ 在 `update_douyin_cookie_config` 路由之后新增：

```python
@app.route('/api/douyin-cookie/check', methods=['POST'])
def check_douyin_cookie():
    """手动触发一次 Cookie 测活，同步返回健康状态（跳过防抖）。"""
    snapshot = cookie_health_service.run_probe(trigger='manual', skip_debounce=True)
    return jsonify(snapshot)
```

⑥ `__main__` 块中 `scheduler_service.start()`（445 行）之前加：

```python
        # 注册 Cookie 健康检查任务（tick 内部自行判断定时/被动触发时机）
        scheduler_service.add_job(
            cookie_health_service.tick,
            'interval',
            seconds=COOKIE_HEALTH_TICK_INTERVAL,
            id='cookie_health_tick',
            name='Cookie健康检查'
        )
```

- [ ] **Step 4: 运行集成测试确认通过**

Run: `./scripts/test.sh integration tests/integration/test_cookie_health_api.py -v`
Expected: 5 个测试全部 PASS

- [ ] **Step 5: 跑全量 unit + integration 确认无回归**

Run: `./scripts/test.sh unit && ./scripts/test.sh integration`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add app.py tests/integration/test_cookie_health_api.py
git commit -m "feat(cookie-health): 接入测活 API、更新后自动探测与定时健康检查"
```

---

### Task 6: 前端展示（状态徽章、测活按钮、失活横幅）

**Files:**
- Modify: `templates/index.html`（CSS、状态条、横幅、Cookie 模态框）
- Modify: `static/js/index.js`（data、computed、轮询、checkCookieHealth）
- Test: `tests/e2e/test_smoke.py`（追加一条冒烟断言）

注意：本页面是 Jinja2 + Vue 共存，所有动态内容必须用 `v-text` / `v-if` / `:class` 指令，禁止 `{{ }}` 插值。

- [ ] **Step 1: index.js — data 与轮询**

① `data` 中 `douyinCookie`（35-38 行）改为：

```javascript
        douyinCookie: {
            configured: false,
            length: 0,
            health: {
                status: 'unknown',
                last_check_time: null,
                last_ok_time: null,
                last_error: null
            }
        },
```

② `data` 中 `cookieSaving: false,`（11 行）之后加：

```javascript
        cookieChecking: false,
```

③ `mounted()` 的 `setInterval` 回调（51-54 行）中追加一行：

```javascript
        setInterval(() => {
            this.loadRooms();
            this.loadStats();
            this.loadCookieConfig();
        }, 5000);
```

④ `loadCookieConfig`（86-94 行）做健壮性兜底（后端旧响应无 health 时不崩）：

```javascript
        async loadCookieConfig() {
            try {
                const response = await fetch('/api/douyin-cookie');
                const data = await response.json();
                if (!data.health) {
                    data.health = { status: 'unknown', last_check_time: null, last_ok_time: null, last_error: null };
                }
                this.douyinCookie = data;
            } catch (error) {
                console.error('加载Cookie配置失败:', error);
            }
        },
```

- [ ] **Step 2: index.js — computed 与 checkCookieHealth**

① 在 `methods:` 之前加 `computed`（与 `data`、`mounted` 平级）：

```javascript
    computed: {
        cookieHealth() {
            return (this.douyinCookie && this.douyinCookie.health) || { status: 'unknown' };
        },
        cookieHealthText() {
            if (!this.douyinCookie.configured) return '未配置';
            const map = { healthy: '正常', suspect: '确认中', dead: '已失活', unknown: '未知' };
            return map[this.cookieHealth.status] || '未知';
        },
        cookiePillClass() {
            if (!this.douyinCookie.configured) return '';
            const map = { healthy: 'is-ready', suspect: 'is-warn', dead: 'is-danger' };
            return map[this.cookieHealth.status] || '';
        }
    },
```

② `methods` 中（`clearCookieConfig` 之后）新增：

```javascript
        async checkCookieHealth() {
            this.cookieChecking = true;
            try {
                const response = await fetch('/api/douyin-cookie/check', { method: 'POST' });
                const data = await response.json();
                if (response.ok) {
                    this.douyinCookie = Object.assign({}, this.douyinCookie, { health: data });
                } else {
                    alert(data.error || '检测失败');
                }
            } catch (error) {
                alert('检测失败: ' + error.message);
            } finally {
                this.cookieChecking = false;
            }
        },
```

③ `updateCookieConfig` 成功分支（172-179 行）改为带 health 与测活结果提示：

```javascript
                if (response.ok) {
                    this.douyinCookie = {
                        configured: data.configured,
                        length: data.length,
                        health: data.health || this.douyinCookie.health
                    };
                    this.cookieForm.cookie = '';
                    this.closeCookieModal();
                    const persistText = data.persisted ? '' : '；但写入 .env 失败，重启后不会保留';
                    const healthText = data.health && data.health.status === 'healthy'
                        ? '，测活通过 ✓' : (data.health && data.health.status === 'dead' ? '，但测活未通过 ✗' : '');
                    alert(`Cookie已更新，已同步 ${data.updated_rooms} 个运行中房间${healthText}${persistText}`);
                } else {
```

- [ ] **Step 3: index.html — CSS 与状态徽章**

① 在 `.status-pill.is-ready .status-dot` 规则（192-195 行）之后加：

```css
        .status-pill.is-warn .status-dot {
            background: #d97706;
            box-shadow: 0 0 0 5px rgba(217, 119, 6, 0.13);
        }

        .status-pill.is-danger .status-dot {
            background: #dc2626;
            box-shadow: 0 0 0 5px rgba(220, 38, 38, 0.13);
        }
```

② Cookie 状态徽章（809-812 行）改为：

```html
                    <div :class="['status-pill', cookiePillClass]">
                        <span class="status-dot"></span>
                        <span>Cookie <strong v-text="cookieHealthText"></strong></span>
                    </div>
```

- [ ] **Step 4: index.html — 失活横幅**

在 `<header class="command-panel">`（798 行）之前、`<div id="app" ...>` 内第一个元素位置加：

```html
        <!-- Cookie 失活提醒横幅 -->
        <div v-if="cookieHealth.status === 'dead'" v-cloak
             class="bg-red-600 text-white text-sm font-medium px-4 py-3 rounded-lg mb-4 flex items-center justify-between">
            <span>⚠️ 抖音 Cookie 已失活，礼物数据可能缺失，请更新 Cookie</span>
            <button @click="openCookieModal" class="underline font-bold ml-4 shrink-0">立即更新</button>
        </div>
```

- [ ] **Step 5: index.html — Cookie 模态框健康状态行 + 测活按钮**

把模态框状态区（1121-1124 行）替换为：

```html
                    <div class="text-sm bg-yellow-50 text-yellow-800 p-3 rounded-lg space-y-1">
                        <div>
                            当前状态：<span class="font-semibold" v-text="douyinCookie.configured ? '已配置' : '未配置'"></span>
                            <span v-if="douyinCookie.length" v-text="'（长度 ' + douyinCookie.length + '）'"></span>
                        </div>
                        <div class="flex items-center flex-wrap gap-1">
                            <span>健康状态：</span>
                            <span class="font-semibold" v-text="cookieHealthText"></span>
                            <span v-if="cookieHealth.last_check_time" class="text-yellow-600"
                                  v-text="'（最后检测 ' + cookieHealth.last_check_time + '）'"></span>
                            <span v-if="cookieHealth.last_error" class="text-red-600" v-text="cookieHealth.last_error"></span>
                            <button @click="checkCookieHealth" :disabled="cookieChecking || !douyinCookie.configured"
                                    class="ml-2 px-2 py-1 text-xs border border-yellow-400 rounded transition-all duration-200 hover:bg-yellow-100 disabled:opacity-50">
                                <span v-text="cookieChecking ? '检测中...' : '测活'"></span>
                            </button>
                        </div>
                    </div>
```

- [ ] **Step 6: e2e 冒烟断言**

在 `tests/e2e/test_smoke.py` 末尾追加：

```python
def test_home_shows_cookie_health_pill(authed_page, base_url):
    """首页 Cookie 状态徽章渲染健康状态文案（未配置/正常/确认中/已失活/未知之一）。"""
    authed_page.goto(base_url)
    pill = authed_page.locator(".status-pill", has_text="Cookie")
    assert pill.is_visible()
    text = pill.inner_text()
    assert any(label in text for label in ("未配置", "正常", "确认中", "已失活", "未知"))
```

- [ ] **Step 7: 运行 e2e 验证**

Run: `./scripts/test.sh e2e`
Expected: 全部 PASS（含新增断言；前置条件与现有 smoke 相同——本地服务在跑）

- [ ] **Step 8: 手动浏览器验证**

1. `./scripts/run.sh` 启动应用，打开 `http://localhost:7654`
2. 顶部 Cookie 徽章显示当前状态
3. 打开"Cookie设置"模态框，点"测活"按钮 → 按钮变"检测中..."，返回后健康状态行更新
4. （可选）`DOUYIN_COOKIE` 改坏后点测活 → 状态变"已失活"，页面顶部出现红色横幅，横幅"立即更新"按钮能打开模态框

- [ ] **Step 9: Commit**

```bash
git add templates/index.html static/js/index.js tests/e2e/test_smoke.py
git commit -m "feat(ui): 首页展示 Cookie 健康状态，支持手动测活与失活横幅提醒"
```

---

### Task 7: 文档同步

**Files:**
- Modify: `CLAUDE.md`（API 端点、定时任务、环境变量、故障排查四个段落）
- Modify: `docs/superpowers/specs/2026-06-10-cookie-health-design.md`（记录两处实现调整）

- [ ] **Step 1: 更新 CLAUDE.md**

① "API 端点"一节"代理配置"小节之后加：

```markdown
### Cookie 配置与测活
- `GET /api/douyin-cookie` - 获取 Cookie 配置状态与健康状态（含 health 字段）
- `POST /api/douyin-cookie` - 更新 Cookie（更新后自动测活，响应含 health）
- `POST /api/douyin-cookie/check` - 手动触发 Cookie 测活
```

② "定时任务 (APScheduler)" 表格加一行：

```markdown
| Cookie 健康检查 | 5min (tick) | 评估被动信号；按 `COOKIE_HEALTH_CHECK_INTERVAL` 定时探测登录态 |
```

③ "环境变量"代码块的"# 监控"段之后加：

```bash
# Cookie 健康检测
COOKIE_HEALTH_CHECK_INTERVAL=1800   # 定时探测间隔(秒)，0=关闭定时探测
COOKIE_HEALTH_GIFT_SILENCE=1800     # 被动信号: 无礼物多久算可疑(秒)
COOKIE_HEALTH_CHAT_ACTIVE=600       # 被动信号: 多久内有弹幕算活跃(秒)
```

④ "故障排查"一节加小节：

```markdown
### 收不到礼物消息
- 大概率是抖音 Cookie 失活（弹幕正常但礼物静默缺失）
- 首页顶部 Cookie 徽章查看健康状态；"Cookie设置"中点"测活"立即确认
- 失活/恢复时间记录在 `system_events` 表（event_type: cookie_dead / cookie_recovered）
- 手动验证: `./scripts/check_cookie.sh`
```

- [ ] **Step 2: 在 spec 文档追加实现备注**

在 `docs/superpowers/specs/2026-06-10-cookie-health-design.md` 末尾追加：

```markdown

## 实现备注（2026-06-10 实施时调整）

1. **Socket.IO 广播改为前端轮询**：首页本无 Socket.IO 连接（纯 fetch + 5 秒轮询），
   为 Cookie 状态引入 Socket.IO 得不偿失；`loadCookieConfig()` 加入现有 5 秒轮询即可实时感知。
2. **调度落地为单个 5 分钟 tick**：每次 tick 评估被动信号（命中且距上次探测 ≥15 分钟则探测）；
   距上次探测 ≥ `COOKIE_HEALTH_CHECK_INTERVAL` 则执行定时探测。语义与原设计等价，单任务更易测试。
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-06-10-cookie-health-design.md
git commit -m "docs: 同步 Cookie 测活功能的 API、环境变量与故障排查说明"
```

---

## 完成标准

- [ ] `./scripts/test.sh unit` 全部通过
- [ ] `./scripts/test.sh integration` 全部通过
- [ ] `./scripts/test.sh e2e` 全部通过
- [ ] `./scripts/check_cookie.sh` 真实 Cookie 实测：活 Cookie → alive，坏 Cookie → dead
- [ ] 手动验证：测活按钮、状态徽章、失活横幅均按预期工作
