# DouyinLiveHub

一个用于学习 WebSocket、Protobuf 解析与实时数据可视化的个人练手项目。围绕直播场景的公开互动信息（弹幕、礼物、点赞）做本地化记录与图表展示，方便按时段回看、按用户名检索个人历史互动。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/Flask-3.0%2B-green)](https://flask.palletsprojects.com/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED)](https://docs.docker.com/compose/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

> 所有截图均为脱敏 Mock 数据，主播昵称、用户名、礼物记录均为虚构，仅供界面演示。

---

## 一、功能介绍

### 1. 首页：本地房间列表

![首页](docs/screenshots/01-home.png)

- 在本地维护一个感兴趣的房间清单，连接异常时按预设策略尝试恢复。
- 顶部 KPI 卡片汇总「主页房间数 / 运行中 / 等待开播 / 已停止 / 已归档」。
- 表格每行展示运行状态 + 直播状态 + 常用操作（查看 / 启停 / 归档），相关网络与凭据配置可在界面即时切换。
- 「归档管理」用于把不再关注的房间从主页移出，原有本地记录保留不删除。

### 2. 房间详情：实时数据看板

![房间详情](docs/screenshots/02-room.png)

- 通过 Socket.IO 把当前会话内的互动数据推送到前端，便于实时观察。
- 顶部卡片汇总当场指标：开播时间、直播时长、本场收入、礼物、弹幕、点赞、峰值观看。
- 右侧贡献榜 + 点赞榜双 Tab，支持按用户名快速过滤本场参与者。
- 用户等级、粉丝团等级以对应图标呈现，便于一眼识别。

### 3. 数据统计：趋势与回顾

![数据统计](docs/screenshots/03-stats.png)

- 时间维度覆盖近 7 天 / 近 30 天 / 按月 / 按年 / 总览 / 自定义日期范围。
- 4 张核心 KPI 卡片 + 副指标行（峰值观看、直播时长、平均时长、场均收入），「场均」分母剔除零值场次，反映真实表现。
- 趋势图可切换收入 / 礼物 / 点赞 / 峰值观看四种指标。
- 下方贡献榜与场次列表跟随筛选条件联动。

### 4. 用户名检索：快速定位

![用户搜索激活](docs/screenshots/04-search.png)

- 在统计页输入用户名，约 350 ms 自动触发模糊匹配，无需手动回车。
- 下拉列出多房间命中候选，附带昵称、所属房间、用户 ID、消息数及累积价值。

### 5. 用户卡片：个人互动回顾

![用户卡片](docs/screenshots/05-user-modal.png)

- 头像、昵称、用户等级、粉丝团等级、关注/粉丝数一目了然。
- 头部 4 张 chip 概览「消息总数 / 弹幕 / 礼物 / 点赞」，与下方 3 个 Tab 在视觉与语义上分离。
- 下方 Tab 可按全部 / 弹幕 / 礼物切换，明细带时间戳与等级标识。

### 其它

- 登录保护：环境变量配置账号密码后，未登录访问直接跳 `/login`。
- 网络配置：支持 HTTP / SOCKS5 代理与运行时 Cookie 配置，方便在不同网络环境下调试。
- 数据保留策略：`DATA_RETENTION_DAYS=0` 表示永久保留本地数据。

---

## 二、快速开始

### 推荐：Docker Compose

```bash
git clone https://github.com/<your-org>/DouyinLiveHub.git
cd DouyinLiveHub
cp .env.example .env          # 至少改一下 SECRET_KEY 和登录密码
docker compose up -d --build
```

启动完成后访问：

```
http://localhost:7654
```

如果 `.env` 里配置了 `AUTH_USERNAME` 和 `AUTH_PASSWORD`，首次访问会跳到 `/login`。

最小 `.env` 关键项：

```env
APP_PORT=7654
SECRET_KEY=换成随机串
AUTH_USERNAME=admin
AUTH_PASSWORD=换成强密码
# MySQL 由 docker-compose 起，本机直连不用改
MYSQL_ROOT_PASSWORD=root123
MYSQL_PASSWORD=douyin123
# 收不到礼物时再填，可在首页 Cookie 设置里覆盖
DOUYIN_COOKIE=
```

### 备选：本地 uv

需要 Python 3.11+、uv、可访问的 MySQL 8。

```bash
cp .env.example .env
./scripts/install.sh          # uv sync
./scripts/init-db.sh          # 创建表
./scripts/run.sh              # 启动 app.py
```

完整环境变量与故障排查见 [`.env.example`](.env.example) 与 [`docs/`](docs/)。

---

## 三、免责声明

本项目仅用于个人学习研究与技术交流，**严禁**用于以下用途：

- 商业谋利、广告获客、二次售卖采集数据。
- 破坏抖音及第三方系统、扰乱平台秩序、规避风控。
- 盗取、贩卖、滥用用户个人信息、隐私或登录凭证。
- 任何违反所在国家或地区法律法规的行为。

使用者需自行承担因不当使用产生的全部法律风险与后果。本项目作者不对滥用造成的任何损害负责。

如本项目内容涉及侵权或其他权益问题，请通过 issue 或邮件联系维护者，确认后会在合理时间内处理。

---

## 致谢

本项目基于 [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) 二次开发，采用 GNU Affero General Public License v3.0。
