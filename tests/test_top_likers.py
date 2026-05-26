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
        self.assertEqual(resp.status_code, 200)


if __name__ == '__main__':
    unittest.main()
