# DouyinLiveHub

抖音直播数据记录站，支持多直播间持续监控、弹幕/礼物/用户贡献记录、直播场次统计、历史数据归档和登录保护。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/uv-supported-purple)](https://github.com/astral-sh/uv)
[![Flask](https://img.shields.io/badge/Flask-3.0%2B-green)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

## 免责声明

本项目仅用于学习研究交流。严禁用于商业谋利、破坏系统、盗取个人信息、侵犯隐私或其他违法违规用途。违反声明使用所产生的一切后果由使用者自行承担。

如涉及侵权或其他权益问题，请联系项目维护者处理。

## 功能

- 多直播间持续监控：添加直播间后自动等待开播，开播后连接，断线后重试或回到轮询。
- 实时数据展示：房间详情页通过 Socket.IO 实时展示弹幕、礼物、观看人数和贡献榜。
- 数据持久化：MySQL 保存弹幕、礼物、用户贡献、统计快照、系统事件和直播场次。
- 数据统计：支持近 7 天、近 30 天、按月份、按年份、总榜和自定义时间范围。
- 用户搜索：按用户名搜索匹配用户，先展示用户列表，点击后查看该用户详细消息和贡献记录。
- 历史数据：主页“归档”直播间不会删除数据，可在历史数据页查看、恢复或永久删除。
- 登录保护：可通过环境变量配置账号密码，防止未授权访问数据。
- Cookie 设置：支持在首页配置抖音 Cookie，解决游客身份可能收不到礼物消息的问题。
- 代理设置：支持 HTTP/SOCKS5 代理配置。
- Docker 部署：内置 MySQL 服务，应用默认监听 `7654`。

## 技术栈

| 组件 | 技术 |
| --- | --- |
| 后端 | Python 3.11+ / Flask / Flask-SocketIO |
| 包管理 | uv |
| 数据库 | MySQL 8.0 |
| ORM | SQLAlchemy |
| 调度 | APScheduler |
| 前端 | Vue 2.6 / TailwindCSS 2.2 |
| 日志 | Loguru |
| 容器 | Docker Compose |

## 快速部署

推荐使用 Docker Compose。

```bash
cp .env.example .env
docker compose up -d --build
```

启动后访问：

```text
http://localhost:7654
```

如果 `.env` 中配置了 `AUTH_USERNAME` 和 `AUTH_PASSWORD`，访问页面时会先进入登录页。

## 环境变量

复制 `.env.example` 为 `.env` 后按需修改。

### 基础配置

```env
APP_PORT=7654
SECRET_KEY=your-secret-key-change-in-production
DEBUG=False
SQLALCHEMY_ECHO=False
```

`SECRET_KEY` 用于 Flask session 加密，生产或长期自用环境请改成随机字符串。
`SQLALCHEMY_ECHO=True` 会把所有 SQL 语句输出到终端，仅建议临时排查数据库问题时开启。

### 登录保护

```env
AUTH_USERNAME=admin
AUTH_PASSWORD=change-this-password
```

同时配置账号和密码后启用登录保护。未登录访问页面会跳转到 `/login`，访问 API 会返回 `401`。

### Socket.IO

```env
SOCKETIO_ASYNC_MODE=threading
```

默认使用 `threading`，避免自动选择 `gevent` 后与普通监控线程、`websocket-client` 混用导致页面请求卡住。

### Docker 构建镜像

```env
DOCKER_PYTHON_IMAGE=python:3.11-slim
DOCKER_UV_IMAGE=ghcr.io/astral-sh/uv:latest
```

如果拉取 `python:3.11-slim` 超时，可以把 `DOCKER_PYTHON_IMAGE` 改成本机已有镜像或可访问的镜像仓库。例如：

```env
DOCKER_PYTHON_IMAGE=python:3.12-slim
```

### MySQL

Docker Compose 会自动创建 MySQL 容器：

```env
MYSQL_ROOT_PASSWORD=root123
MYSQL_DATABASE=douyin_live
MYSQL_USER=douyin
MYSQL_PASSWORD=douyin123
MYSQL_PORT=3306
```

本地非 Docker 运行时，需要配置：

```env
DATABASE_URL=mysql+pymysql://root:password@localhost:3306/douyin_live
```

### 抖音 Cookie

```env
DOUYIN_COOKIE=
```

2026-05 起，游客身份可能收不到礼物推送。若能收到弹幕但收不到礼物消息，请登录 `https://live.douyin.com/`，从浏览器开发者工具的 Network 请求中复制完整 `Cookie`。

也可以在首页点击“Cookie设置”动态更新。保存后会同步运行中的监控实例，并尽量写入 `.env` 以便重启后继续生效。

Cookie 是登录凭证，请只在可信环境使用，不要提交到代码仓库。

### 数据保留

```env
DATA_RETENTION_DAYS=90
```

`0` 表示永久保留。归档直播间不会因为从主页移除而删除数据，但旧数据仍会受 `DATA_RETENTION_DAYS` 的定时清理影响；如果希望历史数据永不自动清理，请设置为：

```env
DATA_RETENTION_DAYS=0
```

### 代理

```env
PROXY_ENABLED=False
PROXY_TYPE=http
PROXY_HOST=127.0.0.1
PROXY_PORT=7890
```

Docker 容器访问宿主机代理时，通常使用：

```env
PROXY_HOST=host.docker.internal
```

## 使用说明

### 添加直播间

1. 打开首页。
2. 点击“添加直播间”。
3. 输入直播间 ID 或抖音号。
4. 系统会创建持续监控任务：未开播时轮询等待，开播后自动连接。

### 启动和停止

- “启动”会重新启用持续监控。
- “停止”会停止当前监控，并关闭自动重连。
- 应用启动后会自动启动配置为持续监控的直播间。

### 查看实时数据

进入房间详情页可以查看：

- 实时弹幕和礼物。
- 当前观看人数、累计观看人数、贡献人数和收入。
- 当场直播统计。
- 贡献榜和用户详情。
- 历史场次。

### 搜索用户

房间详情页和数据统计页都支持用户名搜索：

1. 输入用户名关键词。
2. 系统动态展示匹配用户列表。
3. 点击某个用户后查看该用户的弹幕、礼物、贡献和出现过的直播间。

### 数据统计

数据统计页支持：

- 近 7 天。
- 近 30 天。
- 按月份。
- 按年份。
- 总榜。
- 自定义时间范围。
- 全部房间或单个房间。

统计页会包含归档直播间的数据。

### 归档和历史数据

主页上的“归档”不会删除数据，只会把直播间移出主页并停止监控。归档后的直播间可以在“历史数据”页查看。

历史数据页支持：

- 查看归档直播间的历史数据摘要。
- 进入房间详情继续查看弹幕、礼物、用户和场次。
- 恢复到主页。
- 永久删除。

永久删除会删除该直播间关联的弹幕、礼物、贡献、统计快照、系统事件和直播场次，操作前需要输入直播间 ID 确认。

## 本地开发

本地运行需要 Python 3.11+、uv 和 MySQL。

```bash
cp .env.example .env
uv sync
uv run python -c "from services.data_service import DataService; ds = DataService(); ds.create_tables()"
uv run python app.py
```

访问：

```text
http://localhost:7654
```

也可以使用脚本：

```bash
./scripts/install.sh
./scripts/init-db.sh
./scripts/run.sh
```

## 项目结构

```text
DouyinLiveHub/
├── api/                    # Flask API 路由
│   └── rooms.py
├── crawler/                # 抖音直播抓取与签名逻辑
├── docker/                 # Docker/MySQL 配置
├── models/                 # SQLAlchemy 数据模型
│   └── database.py
├── protobuf/               # Protobuf 协议文件
├── services/               # 数据服务、房间管理、调度服务
├── static/
│   ├── favicon.svg
│   └── js/                 # 前端页面逻辑
├── templates/              # 页面模板
│   ├── index.html          # 首页
│   ├── room.html           # 房间详情
│   ├── stats.html          # 数据统计
│   ├── history.html        # 历史数据
│   └── login.html          # 登录页
├── utils/                  # 日志、状态面板等工具
├── ws_handlers/            # WebSocket 消息处理
├── app.py                  # Flask 应用入口
├── config.py               # 环境变量配置
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── .env.example
```

## 常见问题

### 构建时拉取 `python:3.11-slim` 超时

编辑 `.env`，把 `DOCKER_PYTHON_IMAGE` 改成可访问的镜像或本机已有镜像，然后重新构建：

```bash
docker compose up -d --build
```

### 收不到礼物消息

- 配置 `DOUYIN_COOKIE`。
- 确认 Cookie 来自已登录的 `https://live.douyin.com/`。
- 保存 Cookie 后重启监控，或在首页 Cookie 设置中选择同步运行中的房间。
- 减少同一 Cookie 同时监听的直播间数量，避免触发风控。

### 首页删除后数据没了怎么办

当前版本主页操作已经改为“归档”，不会删除历史数据。历史数据在 `/history` 查看。

旧版本如果已经执行过物理删除，数据库级联删除的数据无法从应用内恢复，需要从数据库备份恢复。

### 归档后还会自动清理吗

会。归档只是不随主页列表显示，不等于绕过数据保留策略。设置 `DATA_RETENTION_DAYS=0` 才是永久保留。

### 忘记登录密码

修改 `.env` 中的 `AUTH_USERNAME` 和 `AUTH_PASSWORD`，然后重启应用：

```bash
docker compose up -d --build
```

### WebSocket 连接失败或疑似风控

- 等待自动重连和轮询。
- 配置代理。
- 降低同时监控的直播间数量。
- 检查 Cookie 是否过期或触发风控。

## 原项目

本项目基于 [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) 进行二次开发。

## 许可证

本项目采用 GNU Affero General Public License v3.0 许可证开源。

如果您修改或发布本程序，需要遵守 AGPL-3.0，包括开放源代码、提供源代码访问方式，并在相同或兼容许可证下发布修改版本。

详情参阅 [LICENSE](LICENSE)。
