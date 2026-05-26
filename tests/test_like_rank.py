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
