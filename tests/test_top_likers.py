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
