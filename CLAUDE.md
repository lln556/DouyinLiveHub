# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

DouyinLiveHub 是一个抖音直播数据记录站，通过逆向工程实现与抖音WebSocket服务器的通信。项目提供两种使用方式：命令行版 (`main.py`) 和 Web版 (`app.py`)。

Web 版本支持**多直播间 24 小时记录**，具有数据持久化、实时 Socket.IO 推送、自动重连等功能。

**重要声明**: 本项目仅供学习研究交流使用，严禁用于商业谋利等非法用途。

---

## 常用命令

### 安装依赖
```bash
pip install -r requirements.txt
```

### 初始化数据库
```bash
python -c "from models.database import Base, engine; Base.metadata.create_all(bind=engine)"
```

### 运行Web版
```bash
python app.py
```
然后访问 `http://localhost:5000`

### 运行命令行版（单房间测试）
```bash
python main.py
```

### 重新生成Protobuf协议文件（如果修改了douyin.proto）
```bash
cd protobuf
protoc -I . --python_betterproto_out=. douyin.proto
```

### 下载用户等级图标（1-75级）
```bash
python utils/download_level.py
```

---

## 核心架构

### 模块导入

**重要**: 爬虫相关代码已整合到 `crawler/` 模块。

```python
# 导入核心爬虫类
from crawler import DouyinLiveWebFetcher, get__ac_signature

# 不要使用旧路径
# from liveMan import DouyinLiveWebFetcher  # ❌ 已废弃
```

### 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    前端层 (Vue.js + TailwindCSS)                 │
│  ┌──────────────┐              ┌──────────────┐                 │
│  │ index.html   │              │ room.html    │                 │
│  │ (房间管理)   │              │ (实时监控)   │                 │
│  └──────┬───────┘              └──────┬───────┘                 │
└─────────┼───────────────────────────────┼───────────────────────┘
          │ Socket.IO                     │ Socket.IO
          ▼                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    应用层 (Flask + Socket.IO)                    │
│  ┌────────────────┐    ┌──────────────────────────────┐         │
│  │ HTTP Routes    │    │ Socket.IO Events              │         │
│  │ /api/rooms     │    │ - connect/disconnect          │         │
│  │ /api/proxy     │    │ - join (房间订阅)             │         │
│  └────────────────┘    └──────────────────────────────┘         │
└─────────────────────────────────────────────────────────────────┘
          │                               │
          ▼                               ▼
┌──────────────────────┐      ┌──────────────────────────────────┐
│  API 层              │      │  服务层 (services/)               │
│  api/rooms.py        │      │  ┌────────────────────────────┐  │
│  - CRUD 房间         │      │  │ RoomManager                 │  │
│  - 启动/停止监控     │      │  │ - add_room()               │  │
│  - 消息/统计查询     │      │  │ - start_room()             │  │
│  - 贡献榜查询        │      │  │ - restart_failed_rooms()   │  │
└──────────────────────┘      │  └──────────┬─────────────────┘  │
                              │  ┌──────────▼─────────────────┐  │
                              │  │ MonitoredRoom (每个房间)   │  │
                              │  │ - _monitor_loop()          │  │
                              │  │ - _poll_room_status()      │  │
                              │  └──────────┬─────────────────┘  │
                              │  ┌──────────▼─────────────────┐  │
                              │  │ DataService                │  │
                              │  │ - SQLAlchemy ORM 封装      │  │
                              │  └──────────┬─────────────────┘  │
                              └─────────────┼─────────────────────┘
                                            │
          ┌─────────────────────────────────┼──────────────────────────────┐
          │                                 │                              │
          ▼                                 ▼                              │
┌──────────────────────┐      ┌──────────────────────────────────┐         │
│  WebSocket 处理层    │      │  调度服务 (SchedulerService)      │         │
│  ws_handlers/        │      │  - 重启失败房间 (30s)             │         │
│  WebDouyinLiveFetcher│      │  - 保存统计快照 (60s)             │         │
│  - 消息解析          │      │  - 清理旧数据 (1h)                │         │
│  - Socket.IO 推送    │      │  - 自动启动24h房间               │         │
│  - 数据库存储        │      └──────────────────────────────────┘         │
└──────────┬───────────┘                                                   │
           │                                                               │
           ▼                                                               │
┌─────────────────────────────────────────────────────────────────────────┤
│  核心抓取层 (crawler/)                                                 │
│  DouyinLiveWebFetcher (crawler/fetcher.py)                             │
│  - WebSocket 连接抖音服务器                                            │
│  - Protobuf 消息解析                                                   │
│  - 签名生成 (crawler/js/sign.js)                                      │
└─────────────────────────────────────────────────────────────────────────┤
           │                                                               │
           ▼                                                               │
┌─────────────────────────────────────────────────────────────────────────┤
│  数据层 (MySQL Database)                                                │
│  表: live_rooms, live_sessions, chat_messages,                         │
│      gift_messages, room_stats, user_contributions, system_events      │
└─────────────────────────────────────────────────────────────────────────┘
```

### 消息处理流程

```
抖音WebSocket服务器
    ↓ (gzip压缩的二进制数据)
PushFrame (协议帧)
    ↓ (解压)
Response (响应容器)
    ↓ (遍历 messages_list)
消息路由器 (根据 method 字段)
    ├─ WebcastChatMessage → _handle_chat_message()
    ├─ WebcastGiftMessage → _handle_gift_message()
    ├─ WebcastRoomUserSeqMessage → _handle_stats_message()
    └─ WebcastControlMessage → _handle_control_message()
    ↓
每个消息处理器:
    ├─ 解析 Protobuf 数据
    ├─ 提取用户信息、内容、等级
    ├─ 保存到数据库 (通过 DataService)
    │  └─ LiveSession 跟踪 (递增统计)
    └─ 通过 Socket.IO 广播到前端
       └─ 事件: `room_{room_id}` (消息) 或 `room_{room_id}_stats` (统计)
    ↓
前端 (room.js):
    └─ 接收 Socket.IO 事件
       └─ 更新 Vue.js 响应式数据
          └─ 自动滚动消息容器
```

### 数据库表关系

| 表 | 用途 | 关联 |
|------|------|------|
| `live_rooms` | 房间元数据 | 主表 |
| `live_sessions` | 直播场次跟踪 | → live_rooms (1:N) |
| `chat_messages` | 弹幕记录 | → live_sessions (1:N) |
| `gift_messages` | 礼物记录 | → live_sessions (1:N) |
| `room_stats` | 周期性统计快照 | → live_rooms (1:N) |
| `user_contributions` | 贡献榜 | → live_rooms (N:1) |
| `system_events` | 系统事件日志 | → live_rooms (1:N) |

**级联删除**: 删除房间时会自动删除所有关联的 sessions、messages、stats、events。

---

## 签名机制

抖音WebSocket连接需要 `signature` 参数，通过以下流程生成：

1. **提取参数** (`generateSignature` in `crawler/fetcher.py`): 从WebSocket URL中提取特定参数
2. **MD5哈希**: 将参数拼接后计算MD5值
3. **JS签名**: 使用 `PyMiniRacer` 执行 `crawler/js/sign.js` 中的 `get_sign()` 函数生成最终签名

**关键**: 当抓取失败时，首先检查签名是否失效，可能需要更新 `crawler/js/sign.js`。

---

## Protobuf协议解析

项目使用两套Protobuf解析库：

| 文件 | 生成工具 | 使用场景 |
|------|----------|----------|
| `protobuf/douyin.py` | betterproto | 主要解析库 (`crawler/fetcher.py`) |
| `protobuf/douyin_pb2.py` | google protobuf | 备用 |

**重要**: `betterproto` 必须使用 2.0 以上版本（当前为 2.0.0b6）。

---

## 支持的消息类型

在 `crawler/fetcher.py` 中定义的消息类型分发逻辑：

| 消息类型 | 解析函数 | 说明 |
|----------|----------|------|
| WebcastChatMessage | _parseChatMsg | 聊天弹幕 |
| WebcastGiftMessage | _parseGiftMsg | 礼物消息（含连击） |
| WebcastMemberMessage | _parseMemberMsg | 用户进场 |
| WebcastLikeMessage | _parseLikeMsg | 点赞消息 |
| WebcastSocialMessage | _parseSocialMsg | 关注消息 |
| WebcastRoomUserSeqMessage | _parseRoomUserSeqMsg | 观众统计 |
| WebcastFansclubMessage | _parseFansclubMsg | 粉丝团消息 |
| WebcastControlMessage | _parseControlMsg | 直播状态（status=3表示结束） |
| WebcastEmojiChatMessage | _parseEmojiChatMsg | 表情包消息 |

---

## 关键技术细节

### 直播间ID获取

从直播间URL `https://live.douyin.com/261378947940` 中，`261378947940` 是 `live_id`。

在 `crawler/fetcher.py` 中，通过正则表达式 `roomId\\":\\"(\d+)\\"` 从HTML中提取真实的 `room_id`。

### 心跳机制

在 `crawler/fetcher.py` 中实现，每5秒发送一次心跳包保持连接。心跳包使用 `PushFrame(payload_type='hb')` 构造。

### Windows编码问题

如果在Windows上遇到GBK编码错误，需要修改Python的 `subprocess.py` 源码。项目中已提供 `patched_popen_encoding` 上下文管理器作为替代方案。

### 连击礼物处理

在 `ws_handlers/handlers.py` 中处理连击礼物：
- **所有礼物都可能是连击的**，不依赖 `send_type` 判断
- 使用 `trace_id` 去重：防止同一消息重复处理
- 使用 `group_id` 组合连击：将相同连击序列的礼物组合起来
- 检查 `combo_count` 字段：如果存在且大于0，说明是连击礼物
- `repeat_end == 1` 表示连击结束
- 使用 `combo_gifts` 字典跟踪连击状态

**礼物数量计算逻辑** (重要):
```
总礼物数量 = combo_count × group_count
```

- `group_count` = 每次连击送的礼物数量（如"人气票*10"中的10）
- `combo_count` = 连击次数（如连击了4次）

**示例**: 用户送"小心心*10"并连击4次
- `group_count = 10`（每次送10个小心心）
- `combo_count = 4`（连击了4次）
- **总礼物数量 = 4 × 10 = 40 个**
- **总价值 = 礼物单价 × 40**

**增量计算** (用于统计和推送):
```
增量数量 = (current_count - last_count) × group_count
增量价值 = 增量数量 × 礼物单价
```

### 礼物去重

使用 `trace_id` 和 `group_id` 组合去重：
1. 首先用 `trace_id` 去重（防止同一消息重复处理）
2. 然后用 `group_id` 把连点的礼物组合起来
- `traceId_list` 限制为 1000 条（保留最新 500 条）

### 自动重连逻辑

```
1. 连接失败
2. 检查 auto_reconnect 标志
3. 检查 reconnect_count < MONITOR_MAX_RETRIES (默认: 5)
4. 是 → 等待 MONITOR_RECONNECT_DELAY (默认: 30s) → 重试
5. 达到最大次数 → 进入轮询模式
   - 每 MONITOR_STATUS_POLL_INTERVAL (默认: 60s) 轮询房间状态
   - 检测到直播 → 重置 reconnect_count 并重新连接
```

### 直播场次跟踪 (LiveSession)

- WebSocket 连接时检查是否有进行中的 `LiveSession`（status='live'）
- 如果存在则继续使用，否则创建新场次
- 每条消息递增统计（收入、礼物、弹幕数）
- 跟踪峰值观看人数
- 收到 `WebcastControlMessage` (status=3) 时结束场次
- 场次结束时更新 `end_time` 和 `peak_viewer_count`

### 前端模板引擎冲突

项目使用 Flask (Jinja2) + Vue.js 组合，两者都使用 `{{ }}` 作为模板分隔符：

| 引擎 | 分隔符 | 处理时机 |
|------|--------|----------|
| Jinja2 | `{{ }}` | 服务端渲染 |
| Vue.js | `{{ }}` | 客户端渲染 |

**解决方案**: 在 HTML 模板中使用 Vue 指令（如 `v-text`、`v-html`）代替 `{{ }}` 插值：

```html
<!-- ❌ 错误：Jinja2 会尝试解析 -->
<span>{{ proxy.enabled ? proxy.host + ':' + proxy.port : '未启用' }}</span>

<!-- ✅ 正确：使用 v-text 指令 -->
<span v-text="proxy.enabled ? proxy.host + ':' + proxy.port : '未启用'"></span>

<!-- ✅ 正确：使用 v-if 指令 -->
<div v-if="proxy.enabled">代理已启用</div>
```

Jinja2 会忽略所有 `v-` 开头的 Vue 指令，只在客户端由 Vue.js 处理。

### 代理支持

**配置** (config.py):
```python
PROXY_ENABLED = os.getenv('PROXY_ENABLED', 'False') == 'True'
PROXY_HOST = os.getenv('PROXY_HOST', '127.0.0.1')
PROXY_PORT = int(os.getenv('PROXY_PORT', '7890'))
PROXY_TYPE = os.getenv('PROXY_TYPE', 'http')  # http or socks5
```

**使用流程**:
1. 前端通过 UI 设置代理 (`/api/proxy` 接口)
2. 配置在运行时更新到内存
3. `MonitoredRoom._monitor_loop()` 创建 `WebDouyinLiveFetcher` 时传递代理配置
4. `WebDouyinLiveFetcher` 将代理配置传递给 `crawler.DouyinLiveWebFetcher`
5. 核心抓取器使用代理连接 WebSocket

---

## API 端点

### 房间管理
- `GET /api/rooms` - 获取房间列表（支持状态筛选）
- `POST /api/rooms` - 添加新房间 (live_id, monitor_type, auto_reconnect)
- `GET /api/rooms/<id>` - 获取房间详情
- `POST /api/rooms/<id>/start` - 开始监控
- `POST /api/rooms/<id>/stop` - 停止监控
- `DELETE /api/rooms/<id>` - 删除房间

### 房间配置
- `PUT /api/rooms/<id>/config` - 更新房间配置（监控类型、自动重连）

### 房间数据
- `GET /api/rooms/<id>/messages` - 获取弹幕/礼物消息
- `GET /api/rooms/<id>/contributors` - 获取贡献榜
- `GET /api/rooms/<id>/stats` - 获取房间统计
- `GET /api/rooms/stats/summary` - 全局统计汇总

### 代理配置
- `GET /api/proxy` - 获取代理配置
- `POST /api/proxy` - 更新代理配置（仅运行时）

---

## Socket.IO 事件

### 客户端 → 服务器
- `join` - 加入房间频道: `{room_id: 123}`

### 服务器 → 客户端
- `room_{room_id}` - 实时消息（弹幕/礼物）
- `room_{room_id}_stats` - 统计更新（含贡献榜）
- `joined` - 加入房间确认

---

## 定时任务 (APScheduler)

| 任务 | 间隔 | 用途 |
|------|------|------|
| 重启失败房间 | 30s | 自动重启 error/stopped 状态的房间 |
| 保存统计快照 | 60s | 持久化当前统计到数据库 |
| 清理旧数据 | 1h | 删除超过 `DATA_RETENTION_DAYS` 的记录 |
| 自动启动24h房间 | 启动时一次 | 启动所有 monitor_type='24h' 的房间 |

---

## 环境变量

```bash
# 数据库
DATABASE_URL=mysql+pymysql://root:password@localhost/douyin_live

# Flask
SECRET_KEY=your-secret-key
DEBUG=False

# 代理
PROXY_ENABLED=False
PROXY_HOST=127.0.0.1
PROXY_PORT=7890
PROXY_TYPE=http

# Socket.IO
SOCKETIO_CORS_ALLOWED_ORIGINS=*

# 监控
MONITOR_RECONNECT_INTERVAL=30
MONITOR_MAX_RETRIES=5
MONITOR_RECONNECT_DELAY=30
MONITOR_STATUS_POLL_INTERVAL=60

# 数据保留
DATA_RETENTION_DAYS=90  # 0 = 永久保留

# 调度器
SCHEDULER_RESTART_FAILED_INTERVAL=30
SCHEDULER_STATS_SNAPSHOT_INTERVAL=60
SCHEDULER_CLEANUPOldData_INTERVAL=3600

# 日志
LOG_LEVEL=INFO
```

---

## 文件说明

| 文件/目录 | 作用 |
|----------|------|
| `crawler/` | 爬虫模块 (DouyinLiveWebFetcher核心类) |
| `crawler/fetcher.py` | 核心抓取逻辑类 `DouyinLiveWebFetcher` |
| `crawler/signature.py` | 签名生成模块 |
| `crawler/js/` | JavaScript签名脚本 |
| `app.py` | Flask Web 应用入口 |
| `main.py` | 命令行版入口示例 |
| `config.py` | 配置文件 |
| `services/` | 服务层 (room_manager, data_service, scheduler_service) |
| `ws_handlers/` | WebSocket 处理器 (WebDouyinLiveFetcher) |
| `models/` | 数据库模型 (SQLAlchemy ORM) |
| `api/` | API 路由 (rooms API) |
| `templates/` | Vue.js 前端模板 |
| `static/js/` | 前端 JavaScript (index.js, room.js) |
| `protobuf/douyin.proto` | Protobuf 协议定义 |
| `protobuf/douyin.py` | betterproto 生成的解析类 |
| `data/level_img/` | 用户等级图标（1-75级PNG） |

---

## 故障排查

### WebSocket连接失败 (502 Bad Gateway)
- 抖音服务器网关问题，通常是临时性的
- 检查是否被风控拦截，可考虑启用代理
- 自动重连机制会处理

### IP 被风控
- 启用代理：设置环境变量 `PROXY_ENABLED=True`
- 或通过 Web 界面"代理设置"配置 Clash 代理 (默认 127.0.0.1:7890)

### room_id 获取失败
- 直播可能已结束，HTML 结构变化
- 错误信息: "No match found for roomId, 直播间可能已结束"
- 已修复缓存问题，每个房间只会请求一次

### 解析消息出错
1. 检查 `crawler/js/sign.js` 是否需要更新
2. 验证 `protobuf/douyin.py` 协议定义是否过时
3. 查看日志中的具体错误类型

### 状态不一致 (应用重启后)
- 应用启动时会自动执行 `_cleanup_stale_statuses()` 清理不一致状态
- 数据库状态为 `monitoring` 但实际未监控的房间会被重置为 `stopped`

---

## 时区处理

项目使用 **东八区 (UTC+8)** 作为标准时区：

```python
# 在 models/database.py 中定义
CHINA_TZ = timezone(timedelta(hours=8))

def get_china_now():
    return datetime.now(CHINA_TZ)
```

所有时间字段默认使用 `get_china_now()`，确保数据库存储的是中国本地时间。

---

## 日志格式

日志器使用 `utils/logger.py` 中的 `get_logger()` 函数创建：

```python
from utils.logger import get_logger
logger = get_logger("module_name", room_display)

# 输出格式: 2026-01-31 19:00:00 | LEVEL | [room_display] module_name | message
```

房间监控的日志上下文会在获取主播信息后更新为 "主播名(live_id)" 格式。
