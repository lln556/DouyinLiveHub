# 系统安全加固、采集稳定性与性能优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 全面修复 DouyinLiveHub 在 Web 鉴权、XSS/CSRF 防护、Cookie 持久化、爬虫签名与未压缩帧解析、WebSocket 重连状态恢复、90 天数据保留保护、SQL N+1/OOM 性能以及前端交互方面的缺陷，保留 MySQL 8.0 并配套容器内存调优与每日热备。

**Architecture:** 对应设计文档 `docs/superpowers/specs/2026-11-21-system-hardening-and-optimization-design.md`，按依赖层次拆分为 10 个独立可验证任务：配置与安全基线 → 鉴权与接口校验 → 爬虫签名与协议帧兼容 → 房间生命周期与并发锁 → 礼物去重与节流推送 → 模型索引与 SQL 原子化/保留保护 → 聚合 N+1/OOM 优化 → 前端静态资源本地化与去 XSS → 前端交互细节修复 → MySQL 调优、热备脚本与存量数据校准。

**Tech Stack:** Python 3.11 + Flask + Flask-SocketIO + SQLAlchemy (PyMySQL / MySQL 8.0) + PyMiniRacer + Vue 2 + TailwindCSS v2 + ECharts 5 + pytest / Playwright

**全局约定：**
- 测试一律通过 `./scripts/test.sh unit|integration|e2e` 运行（内部使用 `uv` 指向独立测试库）。
- 业务时间字段统一使用 `models.database.get_china_now()`（东八区 UTC+8）；内存超时/节流计时使用 `time.monotonic()` 或 `time.time()`。
- 提交信息遵循 Conventional Commits，使用中文 description。
- 每个 Task 完成后跑通对应单测/集成测试再进入下一 Task。

---

### Task 1: 配置安全基线、密钥持久化与 Cookie `.env` 优先读取

**Files:**
- Modify: `config.py`
- Modify: `app.py` (`update_env_value`)
- Modify: `docker-compose.yml` (`environment` 中的 `DOUYIN_COOKIE` 注入方式)
- Test: `tests/test_config_and_env.py` (New)

- [ ] **Step 1: 在 `tests/test_config_and_env.py` 编写配置安全与 `.env` 持久化失败测试**
  - 验证 `.env` 中的非空 `DOUYIN_COOKIE` 能覆盖空环境变量；
  - 验证 `SECRET_KEY` 为默认值时自动生成/加载持久化随机密钥文件 `data/.secret_key`；
  - 验证 `update_env_value` 剥离 `Cookie:` 前缀、安全处理含 `$` 和引号的 Cookie、并生成 `0600` 权限文件。

- [ ] **Step 2: 修改 `config.py` 修复密钥默认值、Cookie 覆盖及安全默认项**
  - 使用 `dotenv_values(BASE_DIR / '.env')` 解析 `DOUYIN_COOKIE`，优先取 `.env` 中最新动态写入的值；
  - 新增 `_resolve_secret_key()`：若环境变量命中默认占位串（`'your-secret-key-change-in-production'`、`'change-this-secret-key-in-production'`、`'your-secret-key'` 或空），从 `BASE_DIR / 'data' / '.secret_key'` 读取或用 `secrets.token_hex(32)` 写入（权限 `0600`）；
  - 补充 `SESSION_COOKIE_HTTPONLY = True`、`SESSION_COOKIE_SAMESITE`（默认 `'Lax'`）、`SESSION_COOKIE_SECURE`（默认 `False`，支持反代按需开启），并将 `SOCKETIO_CORS_ALLOWED_ORIGINS` 默认收紧为同源 `os.getenv('SOCKETIO_CORS_ALLOWED_ORIGINS') or None`。

- [ ] **Step 3: 重写 `app.py` 中的 `update_env_value()` 与 `update_douyin_cookie_config()`**
  - 引入模块级 `_env_write_lock = threading.Lock()`；
  - 读取 Cookie 时剥离可选的前导 `Cookie:`（不区分大小写）；
  - 使用单引号或规范转义 `$`（防止 `python-dotenv` 在双引号内做变量插值截断 Cookie），先写临时文件并 `os.chmod(..., 0o600)` 后 `os.replace()` 原子替换 `.env`；
  - 同步调整 `docker-compose.yml` 中 `app.environment`：移除空默认值 `- DOUYIN_COOKIE=${DOUYIN_COOKIE:-}` 对 `.env` 的遮蔽。

- [ ] **Step 4: 运行测试验证**
  ```bash
  ./scripts/test.sh unit tests/test_config_and_env.py
  ```

---

### Task 2: Web 鉴权加固、移除 `shutdown` 事件、CSRF 同源保护与 API 参数校验

**Files:**
- Modify: `app.py`
- Modify: `api/rooms.py`
- Modify: `templates/index.html`, `templates/room.html`, `templates/stats.html`, `templates/history.html` (退出登录改为 `POST` 表单)
- Test: `tests/integration/test_security_and_validation.py` (New)

- [ ] **Step 1: 编写安全与校验集成测试 `tests/integration/test_security_and_validation.py`**
  - 验证非 ASCII 账号/密码登录不会触发 `TypeError` 500；
  - 验证连续错误登录触发 `429` 限流并记日志；
  - 验证 `get_safe_next('/\\evil.com')` 与 `'//evil.com'` 被重置为 `'/'`；
  - 验证密码配置变更后旧 Session Cookie 自动失效；
  - 验证跨站 `Origin` 发起的 `POST /api/proxy` / `POST /api/rooms` 返回 `403`；
  - 验证 `POST /api/proxy` 拒绝非法协议/端口/包含 `@` 或 `/` 的 host；
  - 验证 `GET /logout` 拒绝，仅允许 `POST /logout`；
  - 验证非法 `live_id`、负数 `limit`、`limit=0`、`page_size=0` 返回 `400` 而非 `500` 除零或堆栈泄漏。

- [ ] **Step 2: 加固 `app.py` 路由、登录、Socket.IO 与启动日志**
  - 删除 `@socketio.on('shutdown')`（`app.py:449-456`）；
  - 在 `app.py` 配置 `SESSION_COOKIE_*`，`handle_connect()` 与 `handle_join(data)` 校验 `is_authenticated()` 及 `isinstance(data, dict)`；
  - `login()` 使用 SHA-256 摘要后 `secrets.compare_digest`，添加每 IP 5 分钟 8 次失败限流器与 `logger.warning` 审计日志，登录成功写入 `session['auth_fp']` 并在 `is_authenticated()` 校对；
  - `get_safe_next()` 先 `replace('\\', '/')` 并拒绝 `//` 前缀；
  - `/logout` 改为 `methods=['POST']`，同步更新 4 个页面模板顶栏的退出控件为 `<form method="post" action="/logout">`；
  - `before_request` 中为非安全 HTTP 方法校验 `Origin` / `Referer` 与 `request.host` 同源（忽略无头非浏览器空 Origin 请求或可通过配置开关控制，阻断浏览器 CSRF）；
  - `POST /api/proxy` 校验 `data` 为 `dict`、`type in ('http', 'socks5')`、`1 <= port <= 65535`、`host` 合法；
  - 启动日志 `app.py:477` 使用 `sqlalchemy.engine.make_url(config.DATABASE_URL).render_as_string(hide_password=True)` 脱敏；
  - 抽取 `_register_scheduler_jobs()` 在应用初始化路径幂等挂载定时任务，并注册 `signal.SIGTERM` / `signal.SIGINT` 处理器执行优雅退出。

- [ ] **Step 3: 收紧 `api/rooms.py` 输入校验与脱敏报错**
  - `live_id` 校验正则改为 `r'^[A-Za-z0-9_.\-]{1,50}$'`；
  - 新增 `_parse_int_param(raw, name, default, min_val, max_val)` 解析分页及数字参数，`ValueError` 返回 `400`；
  - 检查并替换所有 25 处 `return jsonify({'error': str(e)}), 500`，服务端保留 `logger.exception`，向客户端返回通用安全错误信息。

- [ ] **Step 4: 运行测试验证**
  ```bash
  ./scripts/test.sh integration tests/integration/test_security_and_validation.py
  ```

---

### Task 3: 签名时间戳修复、`MiniRacer` 线程本地复用与未压缩 `PushFrame` 兼容

**Files:**
- Modify: `crawler/signature.py`
- Modify: `crawler/fetcher.py`
- Modify: `ws_handlers/handlers.py`
- Test: `tests/test_crawler_protocol.py` (New)

- [ ] **Step 1: 编写协议与签名回归测试 `tests/test_crawler_protocol.py`**
  - 验证 `get__ac_signature` 连续调用不沿用模块导入期冻结时间戳；`one_nonce=None` 不抛 `TypeError`；
  - 验证 `decompress_push_frame_payload` 同时兼容 gzip 压缩载荷（魔数 `\x1f\x8b`）与未压缩裸 Protobuf 载荷，以及空载荷跳过；
  - 验证 `get_room_status()` 在 HTTP 502 / 403 / 空响应 / 非 JSON / 缺少 `room_status` 时返回 `None` 而非 `False`，仅在 `room_status != 0` 时返回 `False`；
  - 验证 `_get_anchor_info_from_html` 不会误匹配页面非 `owner` 区块的无关 `"id"`。

- [ ] **Step 2: 修复 `crawler/signature.py`**
  - 将 `one_time_stamp: int = int(time.time())` 改为 `one_time_stamp: int = None`，函数首部动态获取 `int(time.time())` 并兜底 `one_nonce = one_nonce or ''`。

- [ ] **Step 3: 优化 `crawler/fetcher.py` 与 `ws_handlers/handlers.py`**
  - 模块级缓存读取 `a_bogus.js` 与 `sign.js` 源码字符串，通过 `threading.local()` 维护每线程复用的 `MiniRacer` 上下文（加 `try/except` 在异常时重建），移除 `get_a_bogus()` 中每次启动 `node` 子进程的 `execjs.compile` 路径及未使用且不安全的 `patched_popen_encoding`；
  - `generateSignature` 异常或返回空值时主动抛异常，避免生成 `signature=None` 无效 URL；
  - 实现 `decompress_push_frame_payload(package)` 并同时应用于 `ws_handlers/handlers.py:_wsOnMessage` 与 `crawler/fetcher.py:_wsOnMessage`；
  - `get_room_status()` 调用 `resp.raise_for_status()`，将风控空响应、非 JSON 与缺字段统一返回 `None`；
  - 收紧 `_get_anchor_info_from_html()` 正则限定于 `owner` 字段上下文内提取 `nickname` 和 `id_str`/`id`；
  - 新增 `close()` 关闭 `self.session`，在 `stop()` 中对 `self.ws` 判空并关闭 `ws.sock`。

- [ ] **Step 4: 运行测试验证**
  ```bash
  ./scripts/test.sh unit tests/test_crawler_protocol.py
  ```

---

### Task 4: 房间生命周期优雅退出、连接池释放、并发锁与日志/Cookie 检测优化

**Files:**
- Modify: `services/room_manager.py`
- Modify: `services/scheduler_service.py`
- Modify: `services/cookie_health.py`
- Modify: `utils/logger.py`
- Modify: `utils/status_display.py`
- Test: `tests/test_room_manager_lifecycle.py` (Extend), `tests/test_cookie_health_service.py` (Extend)

- [ ] **Step 1: 补充生命周期与并发回归测试用例**
  - 验证 `room_manager.shutdown()` 调用 `stop(manual=False)` 后数据库保留 `auto_reconnect=True`；
  - 验证 `_poll_room_status()` 每轮结束后关闭 `temp_fetcher` 释放 `requests.Session`；
  - 验证并发增删 `active_rooms` 时 `scheduler_service._save_stats_snapshot` 与 `get_all_rooms_status` 不抛 `RuntimeError`；
  - 验证 `CookieHealthService` 探测耗时网络 I/O 不持有状态锁，且 `dead -> unconfigured -> healthy` 正确记录事件。

- [ ] **Step 2: 更新 `services/room_manager.py`**
  - 为 `MonitoredRoom` 添加 `data_lock = threading.RLock()`，统一保护 `user_contributions`、`combo_gifts`、`stats` 与去重缓存；
  - `MonitoredRoom.stop(self, manual: bool = True)`：当 `manual=False` 时仅置 `shutdown_event`、停 `fetcher` 并更新 `status='stopped'`，**不修改** `auto_reconnect=False` 也不派生后台检查线程；`room_manager.shutdown()` 传入 `manual=False` 并做短 `join(timeout=3)`；
  - `_monitor_loop()`：`last_connect_time` 仅在真正准备建连/已开播后记录；重连等待改用 `self.shutdown_event.wait(delay)`（含退避与 `apply_jitter`）；建连前再复检 `shutdown_event` 防止僵尸连接；线程 `finally` 中关闭 `self.fetcher` 的 HTTP Session；
  - `_poll_room_status()`：在 `finally:` 调用 `temp_fetcher.close()`，修复 `temp_fetcher.status_error_message` 属性引用，若获取到了 `anchor_info` 顺带刷新 `LiveRoom`；
  - `check_and_end_session_if_offline()` 仅当当前有活跃场次或刚达阈值时触发结束逻辑，避免夜间离线无场次时每分钟打 INFO 日志与查库；
  - `update_douyin_cookie()` 仅对正在监控（或非纯静态等待）且需要刷新的房间置标记，释放全局锁后执行网络/Socket 操作；
  - `get_all_rooms_status()` 与 `get_display_status()` 先在 `self.lock` 下拷贝房间快照并批量查库。

- [ ] **Step 3: 更新 `scheduler_service.py`、`cookie_health.py`、`utils/logger.py`、`utils/status_display.py`**
  - `scheduler_service.py`：在 `room_manager.lock` 下获取 `active_rooms` 快照再保存统计快照；
  - `cookie_health.py`：`tick()` 读取配置前尊重 `COOKIE_HEALTH_CHECK_INTERVAL == 0` 语义；`run_probe()` 将 `probe_douyin_cookie` 移出锁外执行，状态迁移与 `snapshot()` 全程加锁；`dead -> unconfigured` 置标记使再次恢复 `healthy` 时也能记 `cookie_recovered`；
  - `utils/logger.py`：文件 sink 加 `enqueue=True`，`error_{time}.log` 启用按周期与大小混合轮转使过期自动清理生效；
  - `utils/status_display.py`：Rich 模式循环首轮后同样置 `self._first_run = False`。

- [ ] **Step 4: 运行测试验证**
  ```bash
  ./scripts/test.sh unit tests/test_room_manager_lifecycle.py tests/test_cookie_health_service.py tests/test_websocket_watchdog.py
  ```

---

### Task 5: 礼物精准去重、跨重连状态恢复与 1 秒节流实时推送

**Files:**
- Modify: `services/room_manager.py` (在 `MonitoredRoom` 承载跨重连去重缓存与 `gift_users`)
- Modify: `ws_handlers/handlers.py`
- Test: `tests/test_like_delta.py` (Extend), `tests/test_gift_reconnection_dedupe.py` (New)

- [ ] **Step 1: 编写重连去重与状态恢复测试 `tests/test_gift_reconnection_dedupe.py`**
  - 模拟 WS 断线新建 `WebDouyinLiveFetcher` 续接活跃 `LiveSession`：验证 `total_income` 与 `max_viewer_count` 正确从 DB 恢复，后续小数值不再向下覆盖峰值；
  - 模拟重连后收到 `need_persist_msg_count=15` 重放的旧礼物（含 `trace_id` 唯一键冲突和空 `trace_id` + `common.msg_id` 场景）：验证 `UserContribution.total_score` 和 `LiveSession.total_income` 均不被重复累加；
  - 模拟 `group_id=0, repeat_end=0` 普通礼物连发与 `repeat_count > 1, group_count = 0` 礼物：验证全部入账不丢单；
  - 模拟乱序回退的连击包（`combo_count=2` 在 `combo_count=3` 之后到达）：验证直接忽略，不插入重复 `GiftMessage` 行；
  - 验证普通多数量礼物事件构造的结构化字段与展示总额正确。

- [ ] **Step 2: 改造 `ws_handlers/handlers.py` 与 `services/room_manager.py`**
  - 在 `MonitoredRoom` 维护 `trace_dedupe`（有界 `deque(maxlen=2000)` + `set`）、`like_dedupe` 及 `gift_user_ids`（按 `user_id`），仅在 `_wsOnOpen` 创建**全新 `LiveSession`** 时清空重置；
  - `_preload_session_data()` 与 `_wsOnOpen()` 续接已有场次时同步恢复 `self.total_income = float(current_session.total_income or 0)`、`self.monitored_room.stats['total_income'] = self.total_income` 与 `self.max_viewer_count = int(current_session.peak_viewer_count or 0)`，并加载当场送礼 `user_id` 到 `gift_user_ids`；
  - 修复 `_handle_stats_message` line 865 `UnboundLocalError: data_service`；
  - 修复 `_handle_gift_message` line 317 `hasattr(gift_msg.user, 'id_str')`；
  - 去重键优先 `trace_id`，次选 `common.msg_id`；`group_id == 0` 视为无 `group_id`；`per_combo_count = (gift_msg.group_count or 0) or (getattr(gift_msg, 'repeat_count', 0) or 0) or 1`；
  - **严格先执行 `save_gift_message()`**，返回值不为 `None` 时再累加 `self.total_income`、`monitored_room.update_contribution` 与 `increment_session_stats`；
  - 乱序连击（`current_count <= last_count`）直接跳过；`combo_gifts` 记录时间戳并在超过 600s 未更新时定期清理；
  - 点赞日志降为 `logger.debug`；新增 1 秒节流 `_emit_room_stats_throttled()`（开播建场次、收到礼物/弹幕/点赞时触发），且在收到礼物与弹幕时推送结构化纯文本字段替代拼接 HTML。

- [ ] **Step 3: 运行测试验证**
  ```bash
  ./scripts/test.sh unit tests/test_gift_reconnection_dedupe.py tests/test_like_delta.py tests/test_like_rank.py tests/test_top_likers.py
  ```

---

### Task 6: 模型索引补齐、SQL 原子更新/Upsert 与 90 天老场次防清零保护

**Files:**
- Modify: `models/database.py`
- Modify: `app.py` (`migrate_database` 幂等索引检查创建)
- Modify: `services/data_service.py`
- Test: `tests/test_retention_and_atomic_stats.py` (New)

- [ ] **Step 1: 编写原子统计与 90 天保留保护测试 `tests/test_retention_and_atomic_stats.py`**
  - 验证 `update_session_stats(peak_viewer_count=500)` 在已有 `peak_viewer_count=5000` 时保持 `5000` 不被写小；
  - 验证多线程并发调用 `increment_session_stats` 和 `update_user_contribution` 不发生丢更新或因唯一键冲突返回 `None`；
  - 验证 `end_live_session` 保留小数精度、同步核对 `total_chat_count`，且当场次已 `ended` 或超过 `DATA_RETENTION_DAYS` 导致明细已清理（`SUM=0` 而 `total_income>0`）时绝不清零；
  - 验证 `cleanup_old_data` 跳过 `status='live'` 场次的消息；
  - 验证 `cleanup_stale_live_sessions` 使用末条消息时间/更新时间而非硬编码 `+2h`。

- [ ] **Step 2: 更新 `models/database.py` 与 `app.py:migrate_database()`**
  - 在模型 `__table_args__` / 列定义中添加 6 组索引：`live_rooms(anchor_id)`、`chat_messages(live_session_id, created_at)`、`chat_messages(user_id, created_at)`、`gift_messages(live_session_id, created_at)`、`user_contributions(total_score)`、`user_contributions(live_id, like_count)`；
  - 在 `app.py:migrate_database()` 中通过 `inspector.get_indexes(table)` 幂等为存量 MySQL 表创建缺失索引。

- [ ] **Step 3: 重构 `services/data_service.py` 写路径与对账逻辑**
  - Engine 增加 `pool_size=15, max_overflow=25`；
  - `create_live_room` 冲突回退调用改为 `self.get_live_room(live_id)`；
  - `increment_session_stats` 改为单条 SQL 原子累加 `UPDATE`；
  - `update_session_stats` 对 `peak_viewer_count` 使用 `func.greatest(func.coalesce(LiveSession.peak_viewer_count, 0), val)`；
  - `update_user_contribution` 使用 `sqlalchemy.dialects.mysql.insert(UserContribution).on_duplicate_key_update(...)`（测试环境若为非 MySQL 方言做安全回退，生产 MySQL 走单条原子 Upsert）；
  - `cleanup_old_data` 排除归属于 `LiveSession.status == 'live'` 的消息，使用 `LIMIT 10000` 循环删除；
  - `end_live_session` 修复浮点截断，核对 `total_chat_count`，并保护老场次及已清理明细场次不被 `0` 覆盖；
  - `cleanup_stale_live_sessions` 计算每场最后活动时间并做保留期内汇总校准；
  - `get_archived_rooms_summary` 改为 `max(gift_income, session_income)` 并用分组查询替代循环 4N 查询；
  - `get_stats_summary` 正确归类 `waiting` 与 `error` 状态，保证指标语义一致。

- [ ] **Step 4: 运行测试验证**
  ```bash
  ./scripts/test.sh unit tests/test_retention_and_atomic_stats.py tests/test_anchor_id_stats.py tests/test_contributor_identity.py
  ```

---

### Task 7: 消除贡献榜/列表 N+1、修复 `get_user_messages` OOM 与统一“礼物数”口径

**Files:**
- Modify: `services/data_service.py`
- Modify: `api/rooms.py`
- Test: `tests/integration/test_stats_summary.py` (Extend), `tests/integration/test_user_search.py` (Extend), `tests/test_stats_dashboard.py` (Extend)

- [ ] **Step 1: 补充查询性能、分页边界与口径一致性测试**
  - 验证 `get_contributors_by_date_range`（`page_size=20`）执行 SQL 查询次数控制在常数级（<= 8 条，不再随行数 `4N` 增长）且 `gift_count` 为礼物总个数 `SUM(gift_count)`；
  - 验证 `get_user_messages(message_type='all', limit=10, offset=10)` 不会把全部历史行全量拉回，且能正确处理按 `session_id` 点赞用户的统计展示（不串入全历史总量）；
  - 验证搜索 `user_name='%'` 或 `'_'` 不会触发全表通配符扫描；
  - 验证跨午夜或带微秒边界的日期范围查询一致性。

- [ ] **Step 2: 优化 `services/data_service.py` 与 `api/rooms.py`**
  - `api/rooms.py:list_rooms` 批量一次性查询所有 `status == 'live'` 场次，消除房间列表 N+1；
  - `get_contributors_by_date_range`：将 `gift_count` 改为 `func.sum(GiftMessage.gift_count)`（`get_session_contributors` 与 `search_users_by_name` 同步对齐）；将循环内的 4 次单用户查询改为基于当页用户集的三次批量 `IN` 查询；修复全局模式计数去重拼接冲突（按 `(live_id, user_id)` 统计或按 `anchor_id` 合并）；
  - `get_user_messages(message_type='all')` 与 `get_all_messages`：先在各自表按 `ORDER BY created_at DESC LIMIT (offset + limit)` 再合并切片，解决 OOM 与全表 filesort；对 `session_id` 非空时的零发言点赞用户不再套用全历史 `UserContribution` 总计冒充当场统计；
  - 为 `ilike` 搜索添加 `_escape_like()` 转义 `\`、`%`、`_`；补齐日期截止时间 `23:59:59.999999` 一致性。

- [ ] **Step 3: 运行集成测试与单元测试验证**
  ```bash
  ./scripts/test.sh unit tests/test_stats_dashboard.py
  ./scripts/test.sh integration tests/integration/test_stats_summary.py tests/integration/test_user_search.py
  ```

---

### Task 8: 前端静态资源本地化（去 CDN）与全站移除 `v-html` 根治 XSS

**Files:**
- Create: `static/vendor/vue.min.js`
- Create: `static/vendor/socket.io.min.js`
- Create: `static/vendor/tailwind.min.css`
- Create: `static/vendor/echarts.min.js`
- Modify: `templates/index.html`, `templates/room.html`, `templates/stats.html`, `templates/history.html`, `templates/login.html`
- Modify: `static/js/room.js`
- Test: `tests/e2e/test_smoke.py` (Extend with XSS & local vendor checks)

- [ ] **Step 1: 下载并核验前端生产压缩依赖到 `static/vendor/`**
  - 从官方发布源下载固定版本压缩文件（`vue@2.6.14/dist/vue.min.js`、`socket.io-client@4.7.5/dist/socket.io.min.js`、`tailwindcss@2.2.19/dist/tailwind.min.css`、`echarts@5.5.1/dist/echarts.min.js`）至 `static/vendor/`，校验非空且文件头正确；
  - 更新 5 个 HTML 模板全部指向 `/static/vendor/...`（同步移除 `login.html` 未使用的 Vue `<script>`），彻底消除对外部 CDN 的依赖；
  - 将模板中的 Tailwind v3 类名替换为 v2 兼容类名，所有外链头像 `<img>` 补全 `referrerpolicy="no-referrer"`。

- [ ] **Step 2: 改造 `templates/room.html` 与 `static/js/room.js` 消除 `v-html`**
  - `room.html` 删除 `<div v-html="msg.content"></div>`，改用结构化 `<span>`/`<img>` 模板配合 `v-text`、`:src`、`:data-user-id`、`:data-user-name` 渲染（等级图标、粉丝团图标、可点击用户名、弹幕内容或礼物详情）；
  - `room.js:handleMessage` 为每条消息分配 `_id: ++this._msgSeq`，`room.html` 消息列表使用 `:key="msg._id"`（贡献榜/点赞榜使用 `:key="contributor.user_id"` / `:key="liker.user_id"`）。

- [ ] **Step 3: 补充并运行 E2E 测试验证无外网依赖与 XSS 防护**
  ```bash
  ./scripts/test.sh e2e
  ```

---

### Task 9: 前端交互 Bug 修复、请求时序保护与轮询优化

**Files:**
- Modify: `templates/room.html`, `templates/stats.html`, `templates/index.html`
- Modify: `static/js/room.js`, `static/js/stats.js`, `static/js/index.js`
- Test: `tests/e2e/test_ui_interactions.py` (New)

- [ ] **Step 1: 编写前端交互回归 E2E 用例 `tests/e2e/test_ui_interactions.py`**
  - 验证搜索框输入后触发 `@blur` 控制台零报错且下拉框自动收起；
  - 验证切换 `"全部房间"` 后点赞榜卡片正确显示；
  - 验证满 500 条消息后继续推送消息不会重复重解析已有 DOM 节点；
  - 验证按钮防连击与弹窗 ESC 关闭。

- [ ] **Step 2: 修复 `room.html` / `room.js` / `stats.html` / `stats.js` / `index.js`**
  - `room.html:723` & `stats.html:364`：`@blur` 改为调用组件方法 `onUserSearchBlur()`；
  - `room.js`：`mounted()` 协调首屏房间信息与 Socket.IO 状态合并（`this.room` 未加载前暂存初始 WS 状态不丢包）；移除 `contributorCount: data.contributors.length` 截断覆写；下播 `currentSession` 归 `null` 时自动调 `loadHistorySessions()` 更新 `lastSession`；对称处理 `likeRankInfo` 下播置空；`messages.filter(...)` 改为 `computed`；滚动加 `requestAnimationFrame` 与 `{ passive: true }`；清除死代码；
  - `stats.js` & `stats.html`：修复切换房间双触发隐藏点赞榜；为 `loadData()` 增加递增 `loadRequestSeq` 防慢请求覆盖；`customStartDate > customEndDate` 自动校对或提示；允许查询当天新开播数据；对 URL 中的 `live_id` 统一 `encodeURIComponent`；场次详情礼物列表展示普通多数量礼物的数量；移除未用的旧 SVG 折线辅助代码；
  - `index.js` & `index.html`：保存 `refreshTimer` 并在 `beforeDestroy` 清理、在 `document.hidden` 时跳过轮询；补齐 `'waiting'` 状态样式；修正代理配置成功弹窗文案；支持回车新增房间、防双击禁用态与 ESC 关闭弹窗。

- [ ] **Step 3: 运行 E2E 全量测试验证**
  ```bash
  ./scripts/test.sh e2e
  ```

---

### Task 10: MySQL 容器调优、每日热备脚本与存量数据无损校准

**Files:**
- Modify: `docker-compose.yml` (`db.command` 内存参数)
- Create: `scripts/backup_db.sh`
- Create: `scripts/repair_historical_data.py` (支持 `--dry-run` 与 `--apply`)
- Modify: `services/scheduler_service.py` (可选接入每日凌晨 `04:30` 热备任务)

- [ ] **Step 1: 更新 `docker-compose.yml` MySQL 启动参数**
  - 在 `db.command` 添加 `--performance-schema=OFF` 与 `--innodb-buffer-pool-size=128M` 降低数据库常驻内存。

- [ ] **Step 2: 新增无锁热备脚本 `scripts/backup_db.sh` 并挂载定时备份**
  - 从环境变量或 `.env` 读取数据库连接配置，使用 `mysqldump --single-transaction --quick --routines --triggers | gzip` 导出至 `data/backups/douyin_live_YYYYMMDD_HHMMSS.sql.gz`（非阻塞线上写入），自动清理 14 天前的旧备份；
  - 在 `SchedulerService` 注册每日 `04:30` 自动执行热备（通过环境开关控制，默认开启）。

- [ ] **Step 3: 新增 `scripts/repair_historical_data.py` 并先 `--dry-run` 后执行存量校准**
  - 执行前先运行 `./scripts/backup_db.sh` 生成基准备份；
  - `--dry-run` 预览待修复记录：
    1. 用同场 `room_stats` 实测最大值向上补齐被断线重连低估的 `live_sessions.peak_viewer_count`；
    2. 将落在场次时间窗口（含下播后 ±5 秒宽限）内的孤儿消息（`live_session_id IS NULL`）回填关联；
    3. 对近 90 天内仍保留完整明细的房间，校准 `user_contributions.total_score` 与 `gift_messages` 求和之间的微差；
  - 确认预览列表准确无误后执行 `--apply` 并再次全库对账核验误差为 0。

- [ ] **Step 4: 全量回归与线上重建验证**
  ```bash
  ./scripts/test.sh all
  docker compose up -d --build
  ./scripts/backup_db.sh
  ```
  - 核查容器 `/healthz`、`logs/app_*.log` 无明文数据库密码、无 `Not a gzipped file` 报错、所有 24h 房间正常恢复监控、Cookie 测活状态正常且重启后不丢失。
