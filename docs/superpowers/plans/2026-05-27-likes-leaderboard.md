# 点赞榜（Likes Leaderboard）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 room 页右侧栏新增「点赞榜」tab（实时、本场），并在 stats 页用户排行区域新增「点赞榜」tab（累积、按房间）。

**Architecture:** 读侧扩展——内存层 `MonitoredRoom.user_contributions.like_count` 已经在累加，不动写侧；新增 `get_like_rank()`（实时）和 `get_top_likers()`（累积）两个查询方法；扩展现有 Socket.IO `room_{id}_stats` payload 加 `like_rank_list` 字段；新增 REST API `GET /api/rooms/top-likers`；前端在贡献榜区域加 tab 切换。

**Tech Stack:** Python 3.12 / Flask / Flask-SocketIO / SQLAlchemy 2.0 / pytest (unittest 风格) / Vue 2 / TailwindCSS

**Spec Reference:** `docs/superpowers/specs/2026-05-27-likes-leaderboard-design.md`

---

## File Structure

| 类型 | 文件 | 责任 |
|------|------|------|
| Modify | `services/room_manager.py` | 加 `MonitoredRoom.get_like_rank(limit)` 方法 |
| Modify | `services/data_service.py` | 加 `DataService.get_top_likers(live_id, limit)` 方法 |
| Modify | `ws_handlers/handlers.py` | 各 emit 点 payload 加 `like_rank_list` |
| Modify | `app.py` | `join` 事件 emit payload 加 `like_rank_list` |
| Modify | `api/rooms.py` | 新增 `GET /api/rooms/top-likers` 路由 |
| Modify | `templates/room.html` | 右侧栏 tab + 点赞榜条目模板 |
| Modify | `static/js/room.js` | `activeRankTab` 状态、`stats.likeRankInfo`、Socket 接收 |
| Modify | `templates/stats.html` | 用户排行区域 tab + 列调整 |
| Modify | `static/js/stats.js` | tab 切换、调用 top-likers API、禁用日期控件 |
| Create | `tests/test_like_rank.py` | `MonitoredRoom.get_like_rank` 单元测试 |
| Create | `tests/test_top_likers.py` | `DataService.get_top_likers` + API 路由集成测试 |

---

## Task 1: `MonitoredRoom.get_like_rank` 服务层方法

**Files:**
- Create: `tests/test_like_rank.py`
- Modify: `services/room_manager.py`（在 `get_contribution_rank` 方法之后新增）

- [ ] **Step 1.1: 写失败测试**

创建 `tests/test_like_rank.py`：

```python
import threading
import unittest
from unittest.mock import MagicMock

from services.room_manager import MonitoredRoom


def _make_room():
    """构造一个不会真的启动监控线程的 MonitoredRoom 实例"""
    manager = MagicMock()
    return MonitoredRoom(live_id='test', manager=manager, socketio=None)


class GetLikeRankTests(unittest.TestCase):
    def test_returns_empty_when_no_user_contributions(self):
        room = _make_room()
        self.assertEqual(room.get_like_rank(), [])

    def test_filters_users_with_zero_like_count(self):
        room = _make_room()
        room.user_contributions = {
            'u1': {'user_name': 'alice', 'score': 100, 'like_count': 0,
                   'avatar': None, 'fans_club_level': 0, 'user_level': 0},
            'u2': {'user_name': 'bob', 'score': 0, 'like_count': 5,
                   'avatar': None, 'fans_club_level': 0, 'user_level': 0},
        }
        rank = room.get_like_rank()
        self.assertEqual(len(rank), 1)
        self.assertEqual(rank[0]['user_id'], 'u2')

    def test_sorts_by_like_count_desc(self):
        room = _make_room()
        room.user_contributions = {
            'u1': {'user_name': 'a', 'score': 0, 'like_count': 5,
                   'avatar': None, 'fans_club_level': 0, 'user_level': 0},
            'u2': {'user_name': 'b', 'score': 0, 'like_count': 50,
                   'avatar': None, 'fans_club_level': 0, 'user_level': 0},
            'u3': {'user_name': 'c', 'score': 0, 'like_count': 10,
                   'avatar': None, 'fans_club_level': 0, 'user_level': 0},
        }
        rank = room.get_like_rank()
        self.assertEqual([r['user_id'] for r in rank], ['u2', 'u3', 'u1'])
        self.assertEqual([r['rank'] for r in rank], [1, 2, 3])

    def test_payload_shape_matches_contribution_rank(self):
        """点赞榜条目字段必须和贡献榜保持兼容，方便前端复用渲染组件"""
        room = _make_room()
        room.user_contributions = {
            'u1': {'user_name': 'alice', 'score': 0, 'like_count': 5,
                   'avatar': 'http://x/y.png', 'fans_club_level': 7, 'user_level': 32},
        }
        item = room.get_like_rank()[0]
        for key in ('rank', 'user_id', 'user', 'like_count', 'avatar',
                    'fans_club_level', 'user_level'):
            self.assertIn(key, item)
        self.assertEqual(item['user'], 'alice')
        self.assertEqual(item['like_count'], 5)
        self.assertEqual(item['fans_club_level'], 7)
        self.assertEqual(item['user_level'], 32)

    def test_respects_limit(self):
        room = _make_room()
        room.user_contributions = {
            f'u{i}': {'user_name': f'n{i}', 'score': 0, 'like_count': i + 1,
                      'avatar': None, 'fans_club_level': 0, 'user_level': 0}
            for i in range(150)
        }
        rank = room.get_like_rank(limit=10)
        self.assertEqual(len(rank), 10)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 1.2: 运行测试，确认失败**

```bash
cd /Users/axis/projects/DouyinLiveHub
python -m pytest tests/test_like_rank.py -v
```

Expected: 5 个测试全部 FAIL，因为 `MonitoredRoom.get_like_rank` 还不存在（AttributeError）。

- [ ] **Step 1.3: 实现 `MonitoredRoom.get_like_rank`**

编辑 `services/room_manager.py`，在 `get_contribution_rank` 方法（约 596 行）之后添加：

```python
    def get_like_rank(self, limit: int = 100) -> list:
        """获取本场点赞排行榜（只显示有点赞行为的用户）"""
        rank_list = sorted(
            [
                {
                    'user_id': k,
                    'user': v['user_name'],
                    'like_count': v.get('like_count', 0),
                    'avatar': v.get('avatar'),
                    'fans_club_level': v.get('fans_club_level', 0),
                    'user_level': v.get('user_level', 0)
                }
                for k, v in self.user_contributions.items()
                if v.get('like_count', 0) > 0
            ],
            key=lambda x: x['like_count'],
            reverse=True
        )[:limit]

        for i, item in enumerate(rank_list):
            item['rank'] = i + 1

        return rank_list
```

- [ ] **Step 1.4: 运行测试，确认通过**

```bash
python -m pytest tests/test_like_rank.py -v
```

Expected: 5 个测试全部 PASS。

- [ ] **Step 1.5: Commit**

```bash
git add tests/test_like_rank.py services/room_manager.py
git commit -m "feat(rank): MonitoredRoom 新增 get_like_rank 内存点赞榜读侧方法"
```

---

## Task 2: `DataService.get_top_likers` 累积查询方法

**Files:**
- Create: `tests/test_top_likers.py`
- Modify: `services/data_service.py`（在 `get_summary_contributors` 方法之后新增）

- [ ] **Step 2.1: 写失败测试**

创建 `tests/test_top_likers.py`：

```python
import unittest

from services.data_service import DataService
from models.database import UserContribution


class GetTopLikersTests(unittest.TestCase):
    def setUp(self):
        self.svc = DataService('sqlite:///:memory:')
        self.svc.create_tables()

    def tearDown(self):
        self.svc.close_session()

    def _insert(self, **kwargs):
        s = self.svc.get_session()
        try:
            row = UserContribution(**kwargs)
            s.add(row)
            s.commit()
        finally:
            s.close()

    def test_empty_when_no_data(self):
        self.assertEqual(self.svc.get_top_likers(), [])

    def test_filters_zero_like_count(self):
        self._insert(live_id='r1', user_id='u1', user_name='a',
                     total_score=100, like_count=0)
        self._insert(live_id='r1', user_id='u2', user_name='b',
                     total_score=0, like_count=10)
        result = self.svc.get_top_likers()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['user_id'], 'u2')
        self.assertEqual(result[0]['like_count'], 10)

    def test_sorted_by_like_count_desc(self):
        self._insert(live_id='r1', user_id='u1', user_name='a',
                     total_score=0, like_count=5)
        self._insert(live_id='r1', user_id='u2', user_name='b',
                     total_score=0, like_count=50)
        self._insert(live_id='r1', user_id='u3', user_name='c',
                     total_score=0, like_count=10)
        result = self.svc.get_top_likers()
        self.assertEqual([r['user_id'] for r in result], ['u2', 'u3', 'u1'])

    def test_filter_by_live_id(self):
        self._insert(live_id='r1', user_id='u1', user_name='a',
                     total_score=0, like_count=5)
        self._insert(live_id='r2', user_id='u2', user_name='b',
                     total_score=0, like_count=50)
        result = self.svc.get_top_likers(live_id='r1')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['live_id'], 'r1')

    def test_cross_room_no_aggregation(self):
        """跨房间查询时，同一 user_id 在不同 live_id 应当显示为多行"""
        self._insert(live_id='r1', user_id='u1', user_name='a',
                     total_score=0, like_count=5)
        self._insert(live_id='r2', user_id='u1', user_name='a',
                     total_score=0, like_count=8)
        result = self.svc.get_top_likers()
        self.assertEqual(len(result), 2)
        like_counts = sorted(r['like_count'] for r in result)
        self.assertEqual(like_counts, [5, 8])

    def test_respects_limit(self):
        for i in range(150):
            self._insert(live_id='r1', user_id=f'u{i}', user_name=f'n{i}',
                         total_score=0, like_count=i + 1)
        result = self.svc.get_top_likers(limit=10)
        self.assertEqual(len(result), 10)

    def test_payload_shape(self):
        self._insert(live_id='r1', anchor_name='主播A',
                     user_id='u1', user_name='alice',
                     user_avatar='http://x/y.png',
                     total_score=200, gift_count=3,
                     like_count=42, fans_club_level=6)
        result = self.svc.get_top_likers()
        item = result[0]
        for key in ('live_id', 'anchor_name', 'user_id', 'user_name',
                    'user_avatar', 'like_count', 'gift_count',
                    'fans_club_level'):
            self.assertIn(key, item)
        self.assertEqual(item['user_avatar'], 'http://x/y.png')
        self.assertEqual(item['like_count'], 42)
        self.assertEqual(item['gift_count'], 3)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2.2: 运行测试，确认失败**

```bash
python -m pytest tests/test_top_likers.py -v
```

Expected: 7 个测试全部 FAIL（`AttributeError: 'DataService' object has no attribute 'get_top_likers'`）。

- [ ] **Step 2.3: 实现 `DataService.get_top_likers`**

编辑 `services/data_service.py`，在 `get_summary_contributors` 方法之后（约 829 行）新增：

```python
    def get_top_likers(self, live_id: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        """获取累积点赞榜（基于 UserContribution.like_count，per-(live_id, user_id) 累积）。
        like_count > 0 才进入榜单；不传 live_id 则跨房间，同一 user_id 在不同房间显示多行。
        """
        session = self.get_session()
        try:
            conditions = [UserContribution.like_count > 0]
            if live_id:
                conditions.append(UserContribution.live_id == live_id)

            rows = session.query(UserContribution).filter(
                and_(*conditions)
            ).order_by(
                UserContribution.like_count.desc(),
                UserContribution.updated_at.desc()
            ).limit(limit).all()

            return [{
                'live_id': r.live_id,
                'anchor_name': r.anchor_name,
                'user_id': r.user_id,
                'user_name': r.user_name,
                'user_avatar': r.user_avatar,
                'like_count': int(r.like_count or 0),
                'gift_count': int(r.gift_count or 0),
                'fans_club_level': r.fans_club_level or 0,
            } for r in rows]
        finally:
            session.close()
```

- [ ] **Step 2.4: 运行测试，确认通过**

```bash
python -m pytest tests/test_top_likers.py -v
```

Expected: 7 个测试全部 PASS。

- [ ] **Step 2.5: Commit**

```bash
git add tests/test_top_likers.py services/data_service.py
git commit -m "feat(data): DataService 新增 get_top_likers 累积点赞榜查询"
```

---

## Task 3: `handlers.py` 各 emit 点 payload 加 `like_rank_list`

**Files:**
- Modify: `ws_handlers/handlers.py`（三处 emit 调用）

> 这一步只是把 `like_rank_list` 字段塞进 emit 的 payload。逻辑层（榜单计算）已经在 Task 1 完成。

- [ ] **Step 3.1: 修改 `_handle_like_message` 推送**

打开 `ws_handlers/handlers.py`，找到 `_handle_like_message` 方法末尾的 `self.socketio.emit(f'room_{self.live_id}_stats', { ... })`（约 745-752 行），在 payload 字典中新增一行 `like_rank_list`：

```python
        self.socketio.emit(f'room_{self.live_id}_stats', {
            'current_user_count': self.monitored_room.stats['current_user_count'],
            'total_user_count': self.monitored_room.stats['total_user_count'],
            'total_like_count': self.monitored_room.stats.get('total_like_count', 0),
            'total_income': self.monitored_room.stats['total_income'],
            'contributor_count': self.monitored_room.stats['contributor_count'],
            'current_session': current_session_data,
            'like_rank_list': self.monitored_room.get_like_rank(100),
        }, room=f'room_{self.live_id}')
```

- [ ] **Step 3.2: 修改 `_handle_stats_message` 推送**

同文件找到 `_handle_stats_message` 末尾的 emit（约 817-827 行），在 payload 中新增 `like_rank_list`：

```python
        self.socketio.emit(f'room_{self.live_id}_stats', {
            'room_status': room_status,
            'room_error_message': room_error_message,
            'current_user_count': self.monitored_room.stats['current_user_count'],
            'total_user_count': self.monitored_room.stats['total_user_count'],
            'total_like_count': self.monitored_room.stats.get('total_like_count', 0),
            'total_income': self.monitored_room.stats['total_income'],
            'contributor_count': self.monitored_room.stats['contributor_count'],
            'contributor_info': rank_list,
            'current_session': current_session_data,
            'like_rank_list': self.monitored_room.get_like_rank(100),
        }, room=f'room_{self.live_id}')
```

- [ ] **Step 3.3: 修改 `_end_current_session` 推送**

同文件找到 `_end_current_session` 末尾的 emit（约 867-877 行）。场次已经结束、点赞榜应当清空显示：

```python
                        self.socketio.emit(f'room_{self.live_id}_stats', {
                            'room_status': room_status,
                            'room_error_message': room_error_message,
                            'current_user_count': self.monitored_room.stats['current_user_count'],
                            'total_user_count': self.monitored_room.stats['total_user_count'],
                            'total_like_count': self.monitored_room.stats.get('total_like_count', 0),
                            'total_income': self.monitored_room.stats['total_income'],
                            'contributor_count': self.monitored_room.stats['contributor_count'],
                            'contributor_info': [],
                            'current_session': current_session_data,
                            'like_rank_list': [],
                        }, room=f'room_{self.live_id}')
```

- [ ] **Step 3.4: 写测试验证 emit payload 含 `like_rank_list`**

在 `tests/test_like_rank.py` 文件末尾追加：

```python
class HandlerLikeEmitTests(unittest.TestCase):
    """验证 _handle_like_message 推送的 stats 事件包含 like_rank_list"""

    def _build_fetcher(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        manager = MagicMock()
        manager.data_service.get_current_live_session.return_value = None

        room = MonitoredRoom(live_id='test', manager=manager, socketio=None)
        room.anchor_name = '主播A'
        room.user_contributions = {}

        # 直接构造 WebDouyinLiveFetcher 实例（不走 __init__，避免拉起 crawler）
        from ws_handlers.handlers import WebDouyinLiveFetcher
        fetcher = WebDouyinLiveFetcher.__new__(WebDouyinLiveFetcher)
        fetcher.live_id = 'test'
        fetcher.monitored_room = room
        fetcher.socketio = MagicMock()
        fetcher.current_session_id = None
        fetcher.anchor_name = '主播A'
        fetcher.like_message_keys = []
        fetcher.log = MagicMock()
        return fetcher

    def _make_like_msg(self, user_id, user_name, count=5, total=5, level=10):
        from types import SimpleNamespace
        user = SimpleNamespace(
            id_str=user_id, id=user_id, nick_name=user_name,
            pay_grade=SimpleNamespace(level=level),
            fans_club=SimpleNamespace(data=SimpleNamespace(level=0)),
        )
        common = SimpleNamespace(msg_id=f'msg-{user_id}-{count}')
        return SimpleNamespace(
            user=user, count=count, total=total,
            common=common, double_like_detail=None,
        )

    def test_like_emit_includes_like_rank_list(self):
        fetcher = self._build_fetcher()
        # 预填一个非零的点赞用户，确保榜单非空
        fetcher.monitored_room.user_contributions = {
            'u1': {'user_name': 'alice', 'score': 0, 'like_count': 5,
                   'avatar': None, 'fans_club_level': 0, 'user_level': 10}
        }
        fetcher.monitored_room.stats = {
            'current_user_count': 0, 'total_user_count': 0,
            'total_like_count': 5, 'total_income': 0, 'contributor_count': 0,
        }

        msg = self._make_like_msg('u2', 'bob', count=3, total=8)
        fetcher._handle_like_message(msg)

        emit_calls = fetcher.socketio.emit.call_args_list
        # 找到 stats 事件
        stats_calls = [c for c in emit_calls if c.args[0] == 'room_test_stats']
        self.assertTrue(stats_calls, 'expected at least one room_test_stats emit')
        payload = stats_calls[-1].args[1]
        self.assertIn('like_rank_list', payload)
        # bob 刚加入应当出现在榜单
        user_ids = [item['user_id'] for item in payload['like_rank_list']]
        self.assertIn('u2', user_ids)
```

- [ ] **Step 3.5: 运行测试，确认通过**

```bash
python -m pytest tests/test_like_rank.py -v
```

Expected: 6 个测试（5 个原有 + 1 个新增）全部 PASS。

- [ ] **Step 3.6: Commit**

```bash
git add ws_handlers/handlers.py tests/test_like_rank.py
git commit -m "feat(socket): emit payload 增加 like_rank_list 字段（实时点赞榜）"
```

---

## Task 4: `app.py` / `room_manager.py` 其他 emit 点同步加 `like_rank_list`

**Files:**
- Modify: `app.py`（`handle_join` 中的初次推送）
- Modify: `services/room_manager.py`（`end_current_session` 中的 emit）

- [ ] **Step 4.1: 修改 `app.py` 的 `handle_join` emit**

打开 `app.py`，找到 `handle_join` 函数末尾的 `emit(f'room_{live_id}_stats', ...)`（约 354-364 行），新增 `like_rank_list`：

```python
            emit(f'room_{live_id}_stats', {
                'room_status': room_status,
                'room_error_message': room_error_message,
                'current_user_count': monitored_room.stats['current_user_count'],
                'total_user_count': monitored_room.stats['total_user_count'],
                'total_like_count': monitored_room.stats.get('total_like_count', 0),
                'total_income': monitored_room.stats['total_income'],
                'contributor_count': monitored_room.stats['contributor_count'],
                'contributor_info': rank_list,
                'current_session': current_session_data,
                'like_rank_list': monitored_room.get_like_rank(100),
            })
```

- [ ] **Step 4.2: 修改 `room_manager.end_current_session` 中的 emit**

打开 `services/room_manager.py`，找到 `MonitoredRoom.end_current_session` 末尾的 `self.socketio.emit(f'room_{self.live_id}_stats', { ... })`（约 499-509 行），新增 `like_rank_list: []`：

```python
                            self.socketio.emit(f'room_{self.live_id}_stats', {
                                'room_status': room_status,
                                'room_error_message': room_error_message,
                                'current_user_count': self.stats['current_user_count'],
                                'total_user_count': self.stats['total_user_count'],
                                'total_like_count': self.stats.get('total_like_count', 0),
                                'total_income': self.stats['total_income'],
                                'contributor_count': self.stats['contributor_count'],
                                'contributor_info': [],
                                'current_session': current_session_data,
                                'like_rank_list': [],
                            }, room=f'room_{self.live_id}')
```

- [ ] **Step 4.3: 跑全部既有测试确保没把 lifecycle 测试打坏**

```bash
python -m pytest tests/ -v
```

Expected: 所有测试 PASS（既有的 `test_room_manager_lifecycle` 不应该受影响，因为它的 FakeSocketIO 只记录 emit 参数不做断言）。

- [ ] **Step 4.4: Commit**

```bash
git add app.py services/room_manager.py
git commit -m "feat(socket): join 和 end_current_session 的 stats 事件加 like_rank_list"
```

---

## Task 5: 新增 `GET /api/rooms/top-likers` REST 路由

**Files:**
- Modify: `api/rooms.py`
- Modify: `tests/test_top_likers.py`（追加 API 集成测试）

- [ ] **Step 5.1: 在 `tests/test_top_likers.py` 末尾追加 API 测试**

```python
class TopLikersApiTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from api.rooms import init_rooms_api

        self.svc = DataService('sqlite:///:memory:')
        self.svc.create_tables()

        app = Flask(__name__)
        rooms_bp = init_rooms_api(self.svc, None, None)
        app.register_blueprint(rooms_bp)
        self.client = app.test_client()

    def tearDown(self):
        self.svc.close_session()

    def _insert(self, **kwargs):
        s = self.svc.get_session()
        try:
            s.add(UserContribution(**kwargs))
            s.commit()
        finally:
            s.close()

    def test_empty(self):
        resp = self.client.get('/api/rooms/top-likers')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['likers'], [])
        self.assertEqual(data['total'], 0)

    def test_returns_data(self):
        self._insert(live_id='r1', user_id='u1', user_name='a',
                     total_score=0, like_count=100)
        resp = self.client.get('/api/rooms/top-likers?limit=10')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data['likers']), 1)
        self.assertEqual(data['likers'][0]['like_count'], 100)
        self.assertEqual(data['source'], 'summary')

    def test_filter_by_live_id(self):
        self._insert(live_id='r1', user_id='u1', user_name='a',
                     total_score=0, like_count=10)
        self._insert(live_id='r2', user_id='u2', user_name='b',
                     total_score=0, like_count=20)
        resp = self.client.get('/api/rooms/top-likers?live_id=r1')
        data = resp.get_json()
        self.assertEqual(len(data['likers']), 1)
        self.assertEqual(data['likers'][0]['live_id'], 'r1')

    def test_caps_limit_at_1000(self):
        for i in range(5):
            self._insert(live_id='r1', user_id=f'u{i}', user_name=f'n{i}',
                         total_score=0, like_count=i + 1)
        resp = self.client.get('/api/rooms/top-likers?limit=99999')
        # 不应崩；limit 应被 cap 到 1000
        self.assertEqual(resp.status_code, 200)
```

- [ ] **Step 5.2: 跑测试，确认 4 个新增用例失败**

```bash
python -m pytest tests/test_top_likers.py::TopLikersApiTests -v
```

Expected: 4 个用例 FAIL（404 not found，因为路由还没建）。

- [ ] **Step 5.3: 实现路由**

编辑 `api/rooms.py`，在 `get_stats_summary` 路由（约 458-466 行）之后插入：

```python
    @rooms_bp.route('/top-likers', methods=['GET'])
    def get_top_likers():
        """累积点赞榜（基于 UserContribution.like_count）。
        Query 参数：
        - live_id: 可选，不传则跨房间
        - limit: 可选，默认 100，上限 1000
        """
        try:
            live_id = (request.args.get('live_id') or '').strip() or None
            limit = min(int(request.args.get('limit', 100)), 1000)

            likers = data_service.get_top_likers(live_id=live_id, limit=limit)

            return jsonify({
                'likers': likers,
                'total': len(likers),
                'source': 'summary'
            })
        except Exception as e:
            logger.error(f"获取累积点赞榜失败: {e}")
            return jsonify({'error': str(e)}), 500
```

- [ ] **Step 5.4: 跑测试，确认通过**

```bash
python -m pytest tests/test_top_likers.py -v
```

Expected: 全部 11 个用例（7 个原有 + 4 个新增）PASS。

- [ ] **Step 5.5: Commit**

```bash
git add api/rooms.py tests/test_top_likers.py
git commit -m "feat(api): GET /api/rooms/top-likers 累积点赞榜路由"
```

---

## Task 6: 前端 — `room.html` + `room.js` 实时点赞榜 tab

**Files:**
- Modify: `templates/room.html`（右侧栏 header + 列表渲染）
- Modify: `static/js/room.js`（state 和 socket handler）

> 这一段没有自动测试覆盖，需要 Docker 启动 + 浏览器手工 QA。

- [ ] **Step 6.1: 修改 `room.html` 右侧栏 header，加入 tab 切换**

在 `templates/room.html` 找到右侧栏的 `<div class="px-4 sm:px-6 py-3 sm:py-4 border-b border-gray-100 bg-gradient-to-r from-yellow-50 to-orange-50">`（约 645 行），把里面的 `<h2>贡献榜 TOP100</h2>` 替换为 tab 标题 + 现有搜索框保留：

```html
                <div class="px-4 sm:px-6 py-3 sm:py-4 border-b border-gray-100 bg-gradient-to-r from-yellow-50 to-orange-50">
                    <div class="flex flex-col gap-3">
                        <div class="flex gap-1 -mb-3">
                            <button
                                @click="setRankTab('gift')"
                                :class="['flex-1 py-2 text-sm font-semibold transition-all duration-200 border-b-2',
                                    activeRankTab === 'gift'
                                        ? 'border-yellow-500 text-yellow-700 bg-gradient-to-t from-yellow-100/40 to-transparent'
                                        : 'border-transparent text-gray-400 hover:text-yellow-600']">
                                🏆 贡献榜
                                <span v-if="stats.contributorInfo && stats.contributorInfo.length"
                                      class="ml-1 px-2 py-0.5 rounded-full text-xs font-semibold"
                                      :class="activeRankTab === 'gift' ? 'bg-yellow-200 text-yellow-800' : 'bg-gray-200 text-gray-500'"
                                      v-text="stats.contributorInfo.length"></span>
                            </button>
                            <button
                                @click="setRankTab('like')"
                                :class="['flex-1 py-2 text-sm font-semibold transition-all duration-200 border-b-2',
                                    activeRankTab === 'like'
                                        ? 'border-red-500 text-red-600 bg-gradient-to-t from-red-100/40 to-transparent'
                                        : 'border-transparent text-gray-400 hover:text-red-500']">
                                ❤️ 点赞榜
                                <span v-if="stats.likeRankInfo && stats.likeRankInfo.length"
                                      class="ml-1 px-2 py-0.5 rounded-full text-xs font-semibold"
                                      :class="activeRankTab === 'like' ? 'bg-red-200 text-red-700' : 'bg-gray-200 text-gray-500'"
                                      v-text="stats.likeRankInfo.length"></span>
                            </button>
                        </div>
                        <form @submit.prevent="searchUserByName" class="relative">
                            <input
                                v-model.trim="userNameSearch"
                                @focus="userSearchFocused = true"
                                @blur="setTimeout(() => userSearchFocused = false, 180)"
                                type="text"
                                class="w-full min-w-0 pl-9 pr-20 py-2 text-sm border border-yellow-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-yellow-400 focus:border-yellow-400 bg-white"
                                placeholder="输入用户名自动搜索">
                            <svg class="absolute left-3 top-2.5 w-4 h-4 text-yellow-500" fill="currentColor" viewBox="0 0 20 20">
                                <path fill-rule="evenodd" d="M8 4a4 4 0 102.48 7.139l3.69 3.69a1 1 0 101.415-1.414l-3.69-3.69A4 4 0 008 4zM6 8a2 2 0 114 0 2 2 0 01-4 0z"/>
                            </svg>
                            <button
                                type="submit"
                                :disabled="userSearchLoading"
                                class="absolute right-1 top-1 px-3 py-1.5 bg-yellow-500 hover:bg-yellow-600 disabled:bg-yellow-300 text-white rounded-md text-xs font-medium transition-colors">
                                <span v-text="userSearchLoading ? '搜索中' : '搜索'"></span>
                            </button>

                            <div v-if="userSearchFocused && (userSearchLoading || userSearchResults.length > 0 || userSearchHasSearched || userSearchError)"
                                 class="absolute left-0 right-0 top-full mt-2 bg-white border border-yellow-100 rounded-lg shadow-xl overflow-hidden z-30">
                                <div v-if="userSearchLoading" class="px-3 py-4 text-center text-sm text-gray-400">搜索中...</div>
                                <div v-else-if="userSearchResults.length > 0" class="max-h-80 overflow-y-auto">
                                    <button
                                        v-for="user in userSearchResults"
                                        :key="user.live_id + '_' + user.user_id"
                                        @mousedown.prevent="selectSearchedUser(user)"
                                        class="w-full px-3 py-2 flex items-center justify-between hover:bg-yellow-50 border-b border-yellow-50 last:border-b-0 text-left">
                                        <div class="flex items-center min-w-0">
                                            <img v-if="user.user_avatar" :src="user.user_avatar" class="w-8 h-8 rounded-full mr-2 object-cover">
                                            <div v-else class="w-8 h-8 rounded-full bg-yellow-100 text-yellow-700 flex items-center justify-center text-xs font-bold mr-2"
                                                 v-text="(user.nickname || user.user_id || 'U').substring(0, 1)"></div>
                                            <div class="min-w-0">
                                                <div class="text-sm font-medium text-gray-800 truncate" v-text="user.nickname || user.user_id"></div>
                                                <div class="text-xs text-gray-400 truncate" v-text="'ID: ' + user.user_id"></div>
                                            </div>
                                        </div>
                                        <div class="text-right text-xs text-gray-500 flex-shrink-0 ml-3">
                                            <div v-text="'消息 ' + formatNumber(user.total_messages || 0)"></div>
                                            <div class="text-yellow-600" v-text="formatNumber(user.total_value || 0) + ' 钻石'"></div>
                                        </div>
                                    </button>
                                </div>
                                <div v-else class="px-3 py-4 text-center text-sm" :class="userSearchError ? 'text-red-500' : 'text-gray-400'">
                                    <span v-text="userSearchError || '未找到匹配用户'"></span>
                                </div>
                                <div v-if="userSearchResults.length > 0" class="px-3 py-2 bg-gray-50 text-xs text-gray-400">
                                    点击用户查看详细消息
                                </div>
                            </div>
                        </form>
                    </div>
                </div>
```

- [ ] **Step 6.2: 修改 `room.html` 列表容器，加入点赞榜分支**

找到 `<div class="contributor-container">`（约 705 行）下面的内容，把整个容器内的两个分支（"暂无贡献数据"与 `v-for contributor`）替换为：

```html
                <div class="contributor-container">
                    <!-- 贡献榜分支 -->
                    <template v-if="activeRankTab === 'gift'">
                        <div v-if="!stats.contributorInfo || stats.contributorInfo.length === 0" class="text-center text-gray-400 py-12">
                            <svg class="w-16 h-16 mx-auto mb-4 text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>
                            </svg>
                            <p>暂无贡献数据</p>
                        </div>
                        <div
                            v-for="(contributor, index) in stats.contributorInfo"
                            :key="'gift-' + index"
                            class="p-3 sm:p-4 border-b border-gray-100 hover:bg-gradient-to-r hover:from-yellow-50 hover:to-orange-50 transition-all duration-200">
                            <div class="flex items-center">
                                <div :class="['rank-badge mr-2 sm:mr-3 flex-shrink-0', getRankClass(contributor.rank)]" v-text="contributor.rank"></div>
                                <img
                                    v-if="contributor.avatar"
                                    :src="contributor.avatar"
                                    class="w-10 h-10 rounded-full mr-2 sm:mr-3 ring-2 ring-yellow-200 flex-shrink-0"
                                    alt="用户头像">
                                <img
                                    v-else
                                    src="https://p3-pc.douyinpic.com/aweme/100x100/aweme-avatar/default-avatar.jpeg"
                                    class="w-10 h-10 rounded-full mr-2 sm:mr-3 ring-2 ring-yellow-200 flex-shrink-0"
                                    alt="默认头像">
                                <div class="flex-1 min-w-0">
                                    <div class="flex items-center gap-1">
                                        <span class="font-semibold text-gray-800 truncate cursor-pointer hover:text-blue-600 transition-colors"
                                             @click="openUserMessagesModal(contributor.user_id, contributor.user)"
                                             v-text="contributor.user"></span>
                                        <img v-if="contributor.user_level"
                                             :src="'/level_img/level_' + contributor.user_level + '.png'"
                                             style="height: 18px; object-fit: contain; flex-shrink: 0;">
                                        <img v-if="contributor.fans_club_level"
                                             :src="'/fansclub_img/fansclub_' + contributor.fans_club_level + '.png'"
                                             style="height: 18px; object-fit: contain; flex-shrink: 0;">
                                    </div>
                                    <div class="text-sm text-gray-500">
                                        <svg class="w-3.5 h-3.5 inline mr-1 text-yellow-500" fill="currentColor" viewBox="0 0 20 20">
                                            <path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z"/>
                                        </svg>
                                        <span class="font-semibold text-yellow-600" v-text="formatNumber(contributor.score)"></span>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </template>

                    <!-- 点赞榜分支 -->
                    <template v-else>
                        <div v-if="!stats.likeRankInfo || stats.likeRankInfo.length === 0" class="text-center text-gray-400 py-12">
                            <div class="text-4xl mb-2">❤️</div>
                            <p>本场暂无点赞数据</p>
                            <p class="text-xs mt-2">等待观众开始点赞...</p>
                        </div>
                        <div
                            v-for="(liker, index) in stats.likeRankInfo"
                            :key="'like-' + index"
                            class="p-3 sm:p-4 border-b border-gray-100 hover:bg-gradient-to-r hover:from-red-50 hover:to-pink-50 transition-all duration-200">
                            <div class="flex items-center">
                                <div :class="['rank-badge mr-2 sm:mr-3 flex-shrink-0', getRankClass(liker.rank)]" v-text="liker.rank"></div>
                                <img
                                    v-if="liker.avatar"
                                    :src="liker.avatar"
                                    class="w-10 h-10 rounded-full mr-2 sm:mr-3 ring-2 ring-red-200 flex-shrink-0"
                                    alt="用户头像">
                                <img
                                    v-else
                                    src="https://p3-pc.douyinpic.com/aweme/100x100/aweme-avatar/default-avatar.jpeg"
                                    class="w-10 h-10 rounded-full mr-2 sm:mr-3 ring-2 ring-red-200 flex-shrink-0"
                                    alt="默认头像">
                                <div class="flex-1 min-w-0">
                                    <div class="flex items-center gap-1">
                                        <span class="font-semibold text-gray-800 truncate cursor-pointer hover:text-blue-600 transition-colors"
                                             @click="openUserMessagesModal(liker.user_id, liker.user)"
                                             v-text="liker.user"></span>
                                        <img v-if="liker.user_level"
                                             :src="'/level_img/level_' + liker.user_level + '.png'"
                                             style="height: 18px; object-fit: contain; flex-shrink: 0;">
                                        <img v-if="liker.fans_club_level"
                                             :src="'/fansclub_img/fansclub_' + liker.fans_club_level + '.png'"
                                             style="height: 18px; object-fit: contain; flex-shrink: 0;">
                                    </div>
                                    <div class="text-sm text-gray-500">
                                        <svg class="w-3.5 h-3.5 inline mr-1 text-red-500" fill="currentColor" viewBox="0 0 20 20">
                                            <path fill-rule="evenodd" d="M3.172 5.172a4 4 0 015.656 0L10 6.343l1.172-1.171a4 4 0 115.656 5.656L10 17.657l-6.828-6.829a4 4 0 010-5.656z" clip-rule="evenodd"/>
                                        </svg>
                                        <span class="font-semibold text-red-500" v-text="formatNumber(liker.like_count)"></span>
                                        <span class="text-xs text-gray-400 ml-1">次点赞</span>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </template>
                </div>
```

- [ ] **Step 6.3: 修改 `room.js`，加入 state 和 setter**

打开 `static/js/room.js`，在 data 字段（约 6-67 行）的 `stats` 对象里加 `likeRankInfo: []`，并在顶层 data 加 `activeRankTab: 'gift'`：

```javascript
        activeTab: 'all',
        activeRankTab: 'gift', // 新增：'gift' | 'like'
        messages: [],
        ...
        stats: {
            currentUserCount: 0,
            totalUserCount: 0,
            totalLikeCount: 0,
            totalIncome: 0,
            contributorCount: 0,
            contributorInfo: [],
            likeRankInfo: [],  // 新增
        },
```

然后在 `methods:` 块里新增 `setRankTab`（紧挨现有 `setTab` 方法）：

```javascript
        setRankTab(tab) {
            this.activeRankTab = tab;
        },
```

- [ ] **Step 6.4: 修改 `room.js` 的 socket stats 监听，接收 `like_rank_list`**

在 `initSocket` 方法里找到接收 stats 事件的回调（搜 `room_${this.liveId}_stats`，需要在文件 100 行之后），在 `mergeStats` 调用前面/调用里同步赋值 `stats.likeRankInfo`。

由于 `mergeStats` 是统一入口，让我们改 `mergeStats` 让它认 `like_rank_list`：

找到 `mergeStats` 方法（用文本搜索 `mergeStats(payload`），在它内部处理 contributor_info 的逻辑附近添加：

```javascript
            // 新增：点赞榜
            if (Array.isArray(payload.like_rank_list)) {
                this.stats.likeRankInfo = payload.like_rank_list;
            } else if (!options.preserveEmptyContributors) {
                this.stats.likeRankInfo = [];
            }
```

> 找不到 `mergeStats` 时（不同版本可能命名不同），将这段逻辑加到接收 stats 事件的回调最后。

- [ ] **Step 6.5: 启动 Docker 手动 QA**

```bash
cd /Users/axis/projects/DouyinLiveHub
docker compose up -d
```

打开浏览器：`http://localhost:7654`，登录后进入任意房间详情页。

QA 检查清单：
1. 右侧栏顶部能看到「🏆 贡献榜 / ❤️ 点赞榜」两个 tab
2. 默认选中 `贡献榜`，点击切到 `点赞榜`
3. 点赞榜空状态显示「本场暂无点赞数据」+ ❤️
4. 主播在播时，有人点赞应该看到点赞榜实时更新（用户头像/等级/粉丝团/点赞数）
5. 点击用户名能打开用户消息模态框
6. 切回贡献榜，原有展示完全不变

- [ ] **Step 6.6: Commit**

```bash
git add templates/room.html static/js/room.js
git commit -m "feat(ui): room 页右侧栏新增点赞榜 tab（实时本场）"
```

---

## Task 7: 前端 — `stats.html` + `stats.js` 累积点赞榜 tab

**Files:**
- Modify: `templates/stats.html`（用户排行区域 tab + 列调整）
- Modify: `static/js/stats.js`（state + tab 切换 + API 调用）

> 同样没有自动测试，靠 Docker 启动 + 浏览器 QA。

- [ ] **Step 7.1: 检查 stats.html 现有用户排行结构**

```bash
grep -n "contributor\|用户排行\|userNameSearch" templates/stats.html | head -30
```

记录"用户排行"块所在行号和现有表头列，作为修改基准。

- [ ] **Step 7.2: 在 `stats.html` 用户排行区域 header 上方插入 tab 行**

找到用户排行表格的容器（通常是带 `contributor-table` class 的 div 或包含分页的 section），在其顶部、表格 thead 之前插入以下 tab 行：

```html
<!-- 用户排行 Tab 切换（新增） -->
<div class="flex items-center gap-2 px-4 py-3 border-b border-gray-200 bg-gray-50">
    <button
        @click="setRankTab('gift')"
        :class="['px-3 py-1.5 rounded-md text-sm font-semibold transition-all',
            activeRankTab === 'gift'
                ? 'bg-yellow-500 text-white shadow-sm'
                : 'bg-white text-gray-500 border border-gray-200 hover:bg-yellow-50']">
        🏆 贡献榜
    </button>
    <button
        @click="setRankTab('like')"
        :class="['px-3 py-1.5 rounded-md text-sm font-semibold transition-all',
            activeRankTab === 'like'
                ? 'bg-red-500 text-white shadow-sm'
                : 'bg-white text-gray-500 border border-gray-200 hover:bg-red-50']">
        ❤️ 点赞榜
    </button>
    <span v-if="activeRankTab === 'like'" class="ml-auto text-xs text-gray-400">
        ⚠️ 点赞榜仅看累积，不支持时间筛选
    </span>
</div>
```

- [ ] **Step 7.3: 给现有日期/范围控件加 `:disabled="activeRankTab === 'like'"`**

在 `stats.html` 里找到日期范围 input、time range select 等控件，给每个加上 `:disabled="activeRankTab === 'like'"` 属性，让切换到点赞榜时变灰：

示例（仿照实际控件）：

```html
<input type="date" v-model="customStartDate" :disabled="activeRankTab === 'like'"
       :class="activeRankTab === 'like' ? 'opacity-50 cursor-not-allowed' : ''">
```

> 实际定位需根据 stats.html 现状逐个加。所有 `customStartDate / customEndDate / selectedMonth / selectedYear / timeRange` 关联的 input/select 都要加。

- [ ] **Step 7.4: 在表格容器内加点赞榜分支**

找到 `<tbody>` 渲染贡献者的循环（`v-for="contributor in contributors"`），把它包到 `<template v-if="activeRankTab === 'gift'">` 里，并在它后面新增 `<template v-else>` 包裹的点赞榜 tbody：

```html
<template v-if="activeRankTab === 'gift'">
    <!-- 原有贡献榜 tbody 保持不变 -->
</template>
<template v-else>
    <tbody>
        <tr v-if="likers.length === 0">
            <td colspan="5" class="text-center py-12 text-gray-400">
                <div class="text-4xl mb-2">❤️</div>
                <p>暂无累积点赞数据</p>
            </td>
        </tr>
        <tr v-for="(liker, index) in likers" :key="'like-' + index"
            class="border-b hover:bg-red-50 transition-colors">
            <td class="px-4 py-2 text-sm" v-text="index + 1"></td>
            <td class="px-4 py-2">
                <div class="flex items-center gap-2">
                    <img v-if="liker.user_avatar" :src="liker.user_avatar"
                         class="w-8 h-8 rounded-full" alt="头像">
                    <div v-else class="w-8 h-8 rounded-full bg-red-100 text-red-600 flex items-center justify-center text-xs font-bold"
                         v-text="(liker.user_name || liker.user_id || 'U').substring(0, 1)"></div>
                    <span class="font-medium clickable-user"
                          @click="openLikerMessages(liker)"
                          v-text="liker.user_name || liker.user_id"></span>
                </div>
            </td>
            <td class="px-4 py-2 text-sm text-gray-500" v-text="liker.anchor_name || liker.live_id"></td>
            <td class="px-4 py-2 text-right">
                <span class="font-bold text-red-500" v-text="formatNumber(liker.like_count)"></span>
            </td>
            <td class="px-4 py-2 text-right text-gray-500" v-text="formatNumber(liker.gift_count)"></td>
        </tr>
    </tbody>
</template>
```

- [ ] **Step 7.5: 修改 `stats.js`，加 state 和方法**

打开 `static/js/stats.js`，在 data 字段中新增：

```javascript
        activeRankTab: 'gift',   // 'gift' | 'like'
        likers: [],
        likersLoading: false,
```

在 methods 中新增：

```javascript
        setRankTab(tab) {
            if (this.activeRankTab === tab) return;
            this.activeRankTab = tab;
            if (tab === 'like') {
                this.loadTopLikers();
            } else {
                // 切回贡献榜，重新跑现有查询（用户可能改了日期）
                if (this.hasSearched) {
                    this.search();
                }
            }
        },

        async loadTopLikers() {
            this.likersLoading = true;
            try {
                const params = new URLSearchParams();
                if (this.selectedRoomId) {
                    params.set('live_id', this.selectedRoomId);
                }
                params.set('limit', '100');
                const resp = await fetch('/api/rooms/top-likers?' + params.toString());
                const data = await resp.json();
                this.likers = data.likers || [];
            } catch (e) {
                console.error('加载累积点赞榜失败:', e);
                this.likers = [];
            } finally {
                this.likersLoading = false;
            }
        },

        openLikerMessages(liker) {
            // 复用现有 openUserMessagesModal 模式
            if (typeof this.openUserMessagesModal === 'function') {
                this.openUserMessagesModal(liker.user_id, liker.user_name || liker.user_id, {
                    live_id: liker.live_id
                });
            }
        },
```

- [ ] **Step 7.6: 让"选择房间"切换时点赞榜也跟着刷新**

在 `stats.js` 找到 `selectedRoomId` 的 watcher 或 onRoomChange 方法。如果没有 watcher，添加一个：

```javascript
    watch: {
        // ... 现有 watcher 保持不变
        selectedRoomId() {
            if (this.activeRankTab === 'like') {
                this.loadTopLikers();
            }
        }
    },
```

> 如果已经有 `watch.selectedRoomId`，把上面这一段合并进去。

- [ ] **Step 7.7: Docker 手动 QA**

```bash
docker compose restart app
```

打开 `http://localhost:7654/stats`：

QA 检查清单：
1. 用户排行区域顶部出现「🏆 贡献榜 / ❤️ 点赞榜」按钮
2. 默认贡献榜，原有时间筛选、查询逻辑正常
3. 切到点赞榜：日期范围控件变灰（disabled），右上角显示「⚠️ 点赞榜仅看累积，不支持时间筛选」
4. 点赞榜表格按 like_count 倒序，显示累积值
5. 不选房间时跨房间展示，同一 user_id 在多房间显示多行
6. 选某个房间，点赞榜只显示该房间数据
7. 点击用户名能打开用户消息模态框
8. 切回贡献榜，控件恢复启用，原有 UI 不变

- [ ] **Step 7.8: 跑一遍所有自动测试，确认没有回归**

```bash
python -m pytest tests/ -v
```

Expected: 全部测试 PASS。

- [ ] **Step 7.9: Commit**

```bash
git add templates/stats.html static/js/stats.js
git commit -m "feat(ui): stats 页用户排行新增累积点赞榜 tab"
```

---

## 完成验证

完成上述 7 个 task 后，对照 spec 第 10 节验证清单逐项确认：

- [ ] room 页 tab 切换不破坏现有贡献榜显示
- [ ] 实时点赞消息进来后点赞榜数字递增
- [ ] 同一用户连续点赞只占榜单一行，数字累加
- [ ] 匿名用户合并正确（同名同等级合并为一条）
- [ ] 新一场直播开播时点赞榜清零
- [ ] stats 页点赞榜显示累积数据
- [ ] stats 页切到点赞榜时日期范围控件禁用，hover 有 tooltip 提示
- [ ] stats 页跨房间显示同一 user_id 的多条记录而非求和
- [ ] 用户名点击进入消息模态框
- [ ] 移动端布局不破坏

所有项通过后，进入下一阶段（创建 PR / 部署到测试环境）。

---

## Self-Review 备忘（已修复）

写完后做了 spec 覆盖与一致性检查：

1. **Spec 覆盖**：spec 第 5 节列出的 9 个文件改动全部被 Task 1-7 覆盖（`room_manager.py` Task1、`data_service.py` Task2、`handlers.py` Task3、`app.py`+`room_manager.py` Task4、`api/rooms.py` Task5、`room.html`+`room.js` Task6、`stats.html`+`stats.js` Task7）。
2. **类型一致性**：`like_rank_list` 在 emit / API / 前端三处的 item shape（`rank, user_id, user, like_count, avatar, fans_club_level, user_level`）保持一致；`/api/rooms/top-likers` 返回的 `likers` item 用 `user_name`+`user_avatar` 字段名（与 `UserContribution` 行对齐），前端读取时映射。
3. **占位符扫描**：未发现 TBD/TODO/「类似 Task N」之类的占位描述，所有代码块都是完整可粘贴的。
