import unittest
from datetime import datetime

from models.database import CHINA_TZ, LiveSession
from services.data_service import DataService


class AnchorIdStatsMergeTests(unittest.TestCase):
    def setUp(self):
        self.svc = DataService('sqlite:///:memory:')
        self.svc.create_tables()
        self.svc.create_live_room(
            '167372342956',
            anchor_name='树.🌱',
            anchor_id='59186137729',
            monitor_type='24h',
            auto_reconnect=True,
            status='archived',
        )
        self.svc.create_live_room(
            'Tree202208',
            anchor_name='树.🌱',
            anchor_id='59186137729',
            monitor_type='24h',
            auto_reconnect=True,
            status='offline',
        )
        self.svc.create_live_room(
            'other-room',
            anchor_name='另一位',
            anchor_id='999',
            monitor_type='24h',
            auto_reconnect=True,
            status='offline',
        )

    def tearDown(self):
        self.svc.drop_tables()
        self.svc.close_session()

    def _session(self, live_id, start, income, gifts=1, chats=1, likes=1, peak=10):
        db = self.svc.get_session()
        try:
            db.add(LiveSession(
                live_id=live_id,
                anchor_name='树.🌱',
                start_time=start,
                end_time=start.replace(hour=start.hour + 1) if start.hour < 23 else start,
                status='ended',
                total_income=income,
                total_gift_count=gifts,
                total_chat_count=chats,
                total_like_count=likes,
                peak_viewer_count=peak,
            ))
            db.commit()
        finally:
            db.close()

    def test_related_live_ids_share_anchor_id(self):
        related = self.svc.get_related_live_ids('Tree202208')
        self.assertEqual(set(related), {'Tree202208', '167372342956'})
        self.assertEqual(related[0], 'Tree202208')
        self.assertEqual(self.svc.get_related_live_ids('other-room'), ['other-room'])

    def test_related_live_ids_without_anchor_stay_self(self):
        self.svc.create_live_room('lonely', anchor_name='孤房')
        self.assertEqual(self.svc.get_related_live_ids('lonely'), ['lonely'])
        self.assertEqual(self.svc.get_related_live_ids('missing'), ['missing'])

    def test_aggregated_stats_sum_related_rooms(self):
        self._session('167372342956', datetime(2026, 5, 1, 20, tzinfo=CHINA_TZ), 100, gifts=4, chats=10, likes=20, peak=80)
        self._session('Tree202208', datetime(2026, 5, 2, 20, tzinfo=CHINA_TZ), 50, gifts=2, chats=5, likes=10, peak=40)
        self._session('other-room', datetime(2026, 5, 2, 21, tzinfo=CHINA_TZ), 999, gifts=9, chats=9, likes=9, peak=9)

        stats = self.svc.get_sessions_aggregated_stats(
            live_id='Tree202208',
            start_date='2026-05-01',
            end_date='2026-05-02',
        )
        self.assertEqual(stats['total_sessions'], 2)
        self.assertEqual(stats['total_income'], 150)
        self.assertEqual(stats['total_gift_count'], 6)
        self.assertEqual(stats['total_chat_count'], 15)
        self.assertEqual(stats['total_like_count'], 30)
        self.assertEqual(stats['peak_viewer_max'], 80)
        self.assertEqual(set(stats['related_live_ids']), {'Tree202208', '167372342956'})

        other = self.svc.get_sessions_aggregated_stats(live_id='other-room')
        self.assertEqual(other['total_income'], 999)
        self.assertEqual(other['total_sessions'], 1)

    def test_session_list_and_date_range_include_related(self):
        self._session('167372342956', datetime(2026, 4, 1, 20, tzinfo=CHINA_TZ), 10)
        self._session('Tree202208', datetime(2026, 5, 10, 20, tzinfo=CHINA_TZ), 20)

        sessions = self.svc.get_room_sessions_stats('Tree202208')
        self.assertEqual({row['live_id'] for row in sessions}, {'Tree202208', '167372342956'})
        self.assertEqual(len(sessions), 2)

        date_range = self.svc.get_room_date_range('167372342956')
        self.assertEqual(date_range, {'min_date': '2026-04-01', 'max_date': '2026-05-10'})

    def test_contributors_merge_same_user_across_related_rooms(self):
        self.svc.save_gift_message(
            '167372342956',
            anchor_name='树.🌱',
            user_id='fan-1',
            user_name='旧粉',
            gift_id='g1',
            gift_name='小心心',
            gift_count=1,
            gift_price=100,
            total_value=100,
            send_type='normal',
            created_at=datetime(2026, 5, 1, 12, tzinfo=CHINA_TZ),
        )
        self.svc.save_gift_message(
            'Tree202208',
            anchor_name='树.🌱',
            user_id='fan-1',
            user_name='新粉',
            gift_id='g2',
            gift_name='小心心',
            gift_count=1,
            gift_price=50,
            total_value=50,
            send_type='normal',
            created_at=datetime(2026, 5, 2, 12, tzinfo=CHINA_TZ),
        )
        self.svc.save_chat_message(
            '167372342956',
            anchor_name='树.🌱',
            user_id='fan-1',
            user_name='旧粉',
            content='hi',
            created_at=datetime(2026, 5, 1, 12, 1, tzinfo=CHINA_TZ),
        )
        self.svc.update_user_contribution(
            '167372342956', '树.🌱', 'fan-1', '旧粉',
            gift_value=100, gift_count=1, like_count=3,
        )
        self.svc.update_user_contribution(
            'Tree202208', '树.🌱', 'fan-1', '新粉',
            gift_value=50, gift_count=1, like_count=7,
        )

        result = self.svc.get_contributors_by_date_range(
            live_id='Tree202208',
            start_date='2026-05-01',
            end_date='2026-05-02',
            page=1,
            page_size=20,
        )
        self.assertEqual(result['total'], 1)
        row = result['contributors'][0]
        self.assertEqual(row['user_id'], 'fan-1')
        self.assertEqual(row['contribution_value'], 150)
        self.assertEqual(row['gift_count'], 2)
        self.assertEqual(row['chat_count'], 1)
        self.assertEqual(row['like_count'], 10)

        summary = self.svc.get_summary_contributors(live_id='167372342956')
        self.assertEqual(summary['total'], 1)
        self.assertEqual(summary['contributors'][0]['contribution_value'], 150)
        self.assertEqual(summary['contributors'][0]['like_count'], 10)

        likers = self.svc.get_top_likers(live_id='Tree202208')
        self.assertEqual(len(likers), 1)
        self.assertEqual(likers[0]['like_count'], 10)

        messages = self.svc.get_user_messages(
            live_id='Tree202208',
            user_id='fan-1',
            start_date='2026-05-01',
            end_date='2026-05-02',
        )
        self.assertEqual(messages['stats']['gift_count'], 2)
        self.assertEqual(messages['stats']['chat_count'], 1)
        self.assertEqual(messages['stats']['like_count'], 10)
        self.assertEqual(messages['stats']['total_value'], 150)

        users = self.svc.search_users_by_name(live_id='Tree202208', user_name='粉')
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]['user_id'], 'fan-1')
        self.assertEqual(users[0]['gift_count'], 2)
        self.assertEqual(users[0]['total_value'], 150)

    def test_all_rooms_contributors_stay_split_by_live_id(self):
        self.svc.save_gift_message(
            '167372342956',
            anchor_name='树.🌱',
            user_id='fan-1',
            user_name='粉',
            gift_id='g1',
            gift_name='小心心',
            gift_count=1,
            gift_price=100,
            total_value=100,
            send_type='normal',
            created_at=datetime(2026, 5, 1, 12, tzinfo=CHINA_TZ),
        )
        self.svc.save_gift_message(
            'other-room',
            anchor_name='另一位',
            user_id='fan-1',
            user_name='粉',
            gift_id='g2',
            gift_name='小心心',
            gift_count=1,
            gift_price=20,
            total_value=20,
            send_type='normal',
            created_at=datetime(2026, 5, 1, 13, tzinfo=CHINA_TZ),
        )
        result = self.svc.get_contributors_by_date_range(
            start_date='2026-05-01',
            end_date='2026-05-01',
        )
        self.assertEqual(result['total'], 2)


class AnchorIdStatsMarkupTests(unittest.TestCase):
    def test_stats_page_mentions_related_room_merge(self):
        with open('templates/stats.html', encoding='utf-8') as f:
            html = f.read()
        with open('static/js/stats.js', encoding='utf-8') as f:
            script = f.read()
        self.assertIn('data-related-rooms-banner', html)
        self.assertIn('同主播已合并', html)
        self.assertIn('mergedRelatedRooms()', script)
        self.assertIn('related_live_ids', script)


if __name__ == '__main__':
    unittest.main()
