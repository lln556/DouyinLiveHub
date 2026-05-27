import unittest
from datetime import datetime

from models.database import CHINA_TZ
from services.data_service import DataService


class ContributorIdentityTests(unittest.TestCase):
    def setUp(self):
        self.svc = DataService('sqlite:///:memory:')
        self.svc.create_tables()
        self.svc.create_live_room(
            'room-1',
            anchor_name='主播A',
            monitor_type='24h',
            auto_reconnect=True,
            status='monitoring',
        )

    def tearDown(self):
        self.svc.close_session()

    def _gift(self, user_name, user_level, total_value, created_at):
        self.svc.save_gift_message(
            'room-1',
            anchor_name='主播A',
            user_id='user-1',
            user_name=user_name,
            user_level=user_level,
            gift_id='gift-1',
            gift_name='小心心',
            gift_count=1,
            gift_price=total_value,
            total_value=total_value,
            send_type='normal',
            created_at=created_at,
        )

    def _chat(self, user_name, user_level, created_at):
        self.svc.save_chat_message(
            'room-1',
            anchor_name='主播A',
            user_id='user-1',
            user_name=user_name,
            user_level=user_level,
            content='hello',
            created_at=created_at,
        )

    def test_range_and_summary_leaderboards_use_latest_profile_fields(self):
        self._gift('30天旧名', 12, 100, datetime(2026, 5, 1, 12, tzinfo=CHINA_TZ))
        self._chat('7天旧名', 20, datetime(2026, 5, 24, 12, tzinfo=CHINA_TZ))
        self._gift('7天礼物旧名', 18, 50, datetime(2026, 5, 25, 12, tzinfo=CHINA_TZ))

        self.svc.update_user_contribution(
            'room-1',
            '主播A',
            'user-1',
            '最新昵称',
            gift_value=150,
            gift_count=2,
            user_avatar='https://example.test/latest.png',
            fans_club_level=8,
            user_level=55,
        )

        seven_day = self.svc.get_contributors_by_date_range(
            live_id='room-1',
            start_date='2026-05-20',
            end_date='2026-05-27',
            page=1,
            page_size=20,
        )['contributors'][0]
        thirty_day = self.svc.get_contributors_by_date_range(
            live_id='room-1',
            start_date='2026-04-27',
            end_date='2026-05-27',
            page=1,
            page_size=20,
        )['contributors'][0]
        summary = self.svc.get_summary_contributors(
            live_id='room-1',
            page=1,
            page_size=20,
        )['contributors'][0]

        for row in (seven_day, thirty_day, summary):
            self.assertEqual(row['nickname'], '最新昵称')
            self.assertEqual(row['user_avatar'], 'https://example.test/latest.png')
            self.assertEqual(row['user_level'], 55)
            self.assertEqual(row['fans_club_level'], 8)

        self.assertEqual(seven_day['contribution_value'], 50)
        self.assertEqual(thirty_day['contribution_value'], 150)
        self.assertEqual(summary['contribution_value'], 150)


if __name__ == '__main__':
    unittest.main()
