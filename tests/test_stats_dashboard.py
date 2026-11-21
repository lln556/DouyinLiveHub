import unittest
from datetime import datetime

from models.database import CHINA_TZ, LiveRoom, LiveSession
from services.data_service import DataService


class StatsDashboardDataTests(unittest.TestCase):
    def setUp(self):
        self.svc = DataService('sqlite:///:memory:')
        self.svc.create_tables()

    def tearDown(self):
        self.svc.drop_tables()
        self.svc.close_session()

    def test_aggregated_stats_include_daily_trend_points(self):
        session = self.svc.get_session()
        try:
            session.add(LiveRoom(live_id='room-1', anchor_name='主播A'))
            session.add_all([
                LiveSession(
                    live_id='room-1',
                    anchor_name='主播A',
                    start_time=datetime(2026, 5, 25, 20, 0, tzinfo=CHINA_TZ),
                    end_time=datetime(2026, 5, 25, 22, 0, tzinfo=CHINA_TZ),
                    status='ended',
                    total_income=100,
                    total_gift_count=5,
                    total_chat_count=50,
                    total_like_count=500,
                    peak_viewer_count=80,
                ),
                LiveSession(
                    live_id='room-1',
                    anchor_name='主播A',
                    start_time=datetime(2026, 5, 25, 23, 0, tzinfo=CHINA_TZ),
                    end_time=datetime(2026, 5, 26, 1, 0, tzinfo=CHINA_TZ),
                    status='ended',
                    total_income=200,
                    total_gift_count=7,
                    total_chat_count=60,
                    total_like_count=600,
                    peak_viewer_count=120,
                ),
                LiveSession(
                    live_id='room-1',
                    anchor_name='主播A',
                    start_time=datetime(2026, 5, 26, 20, 0, tzinfo=CHINA_TZ),
                    end_time=datetime(2026, 5, 26, 21, 0, tzinfo=CHINA_TZ),
                    status='ended',
                    total_income=50,
                    total_gift_count=2,
                    total_chat_count=20,
                    total_like_count=100,
                    peak_viewer_count=90,
                ),
            ])
            session.commit()
        finally:
            session.close()

        stats = self.svc.get_sessions_aggregated_stats(
            live_id='room-1',
            start_date='2026-05-25',
            end_date='2026-05-26',
        )

        self.assertEqual(stats['trend'], [
            {
                'date': '2026-05-25',
                'sessions': 2,
                'income': 300,
                'gift_count': 12,
                'chat_count': 110,
                'like_count': 1100,
                'peak_viewer': 120,
            },
            {
                'date': '2026-05-26',
                'sessions': 1,
                'income': 50,
                'gift_count': 2,
                'chat_count': 20,
                'like_count': 100,
                'peak_viewer': 90,
            },
        ])

    def test_room_date_range_handles_sqlite_date_strings(self):
        session = self.svc.get_session()
        try:
            session.add(LiveRoom(live_id='room-1', anchor_name='主播A'))
            session.add(LiveSession(
                live_id='room-1',
                anchor_name='主播A',
                start_time=datetime(2026, 5, 27, 20, 0, tzinfo=CHINA_TZ),
                status='ended',
            ))
            session.commit()
        finally:
            session.close()

        self.assertEqual(self.svc.get_room_date_range(), {
            'min_date': '2026-05-27',
            'max_date': '2026-05-27',
        })

    def test_averages_ignore_zero_income_and_zero_like_sessions(self):
        session = self.svc.get_session()
        try:
            session.add(LiveRoom(live_id='room-1', anchor_name='主播A'))
            session.add_all([
                LiveSession(
                    live_id='room-1',
                    anchor_name='主播A',
                    start_time=datetime(2026, 5, 25, 20, 0, tzinfo=CHINA_TZ),
                    status='ended',
                    total_income=100,
                    total_like_count=0,
                ),
                LiveSession(
                    live_id='room-1',
                    anchor_name='主播A',
                    start_time=datetime(2026, 5, 26, 20, 0, tzinfo=CHINA_TZ),
                    status='ended',
                    total_income=0,
                    total_like_count=500,
                ),
                LiveSession(
                    live_id='room-1',
                    anchor_name='主播A',
                    start_time=datetime(2026, 5, 27, 20, 0, tzinfo=CHINA_TZ),
                    status='ended',
                    total_income=300,
                    total_like_count=1500,
                ),
            ])
            session.commit()
        finally:
            session.close()

        stats = self.svc.get_sessions_aggregated_stats(live_id='room-1')

        self.assertEqual(stats['total_sessions'], 3)
        self.assertEqual(stats['total_income'], 400)
        self.assertEqual(stats['total_like_count'], 2000)
        self.assertEqual(stats['avg_income_per_active_session'], 200)
        self.assertEqual(stats['avg_likes_per_active_session'], 1000)


    def test_contributor_page_has_constant_queries_and_counts_gift_units(self):
        from sqlalchemy import event
        self.svc.create_live_room('room-1', anchor_name='test', anchor_id='anchor')
        for i in range(25):
            uid = f'u{i}'
            self.svc.save_gift_message('room-1', user_id=uid, user_name='name', gift_name='gift', gift_count=7, gift_price=.5, total_value=3.5)
            self.svc.save_chat_message('room-1', user_id=uid, user_name='name', content='hi')
        sql = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            sql.append(statement)
        event.listen(self.svc.engine, 'before_cursor_execute', capture)
        try:
            result = self.svc.get_contributors_by_date_range('room-1', page_size=20)
        finally:
            event.remove(self.svc.engine, 'before_cursor_execute', capture)
        self.assertLessEqual(len(sql), 8)
        self.assertEqual(result['total'], 25)
        self.assertEqual(len(result['contributors']), 20)
        self.assertTrue(all(r['gift_count'] == 7 and r['chat_count'] == 1 for r in result['contributors']))

    def test_global_contributor_count_uses_pairs_without_separator_collisions(self):
        for rid, uid in [('a_b', 'c'), ('a', 'b_c')]:
            self.svc.create_live_room(rid, anchor_name='test')
            self.svc.save_gift_message(rid, user_id=uid, user_name='name', gift_name='gift', gift_count=1, gift_price=1, total_value=1)
        self.assertEqual(self.svc.get_contributors_by_date_range()['total'], 2)

    def test_mixed_message_pagination_bounds_each_table(self):
        from sqlalchemy import event
        self.svc.create_live_room('room-1', anchor_name='test')
        for i in range(35):
            when = datetime(2026, 11, 21, 12, 0, i, tzinfo=CHINA_TZ)
            self.svc.save_chat_message('room-1', user_id='u', user_name='user', content=str(i), created_at=when)
            self.svc.save_gift_message('room-1', user_id='u', user_name='user', gift_name='gift', gift_count=2, gift_price=1, total_value=2, created_at=when)
        sql = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            sql.append(statement)
        event.listen(self.svc.engine, 'before_cursor_execute', capture)
        try:
            result = self.svc.get_user_messages('room-1', user_id='u', limit=10, offset=10)
            mixed = self.svc.get_all_messages('room-1', limit=10, offset=10)
        finally:
            event.remove(self.svc.engine, 'before_cursor_execute', capture)
        self.assertEqual(len(result['messages']), 10)
        self.assertEqual(len(mixed), 10)
        self.assertEqual(result['pagination']['total'], 70)
        self.assertEqual(result['messages'][0]['created_at'], datetime(2026, 11, 21, 12, 0, 29).isoformat())
        selects = [q for q in sql if 'ORDER BY' in q and ('FROM chat_messages' in q or 'FROM gift_messages' in q)]
        self.assertTrue(selects)
        self.assertTrue(all('LIMIT' in q for q in selects), selects)

    def test_session_only_liker_does_not_show_lifetime_totals(self):
        self.svc.create_live_room('room-1', anchor_name='test')
        live = self.svc.create_live_session('room-1')
        self.svc.update_user_contribution('room-1', 'test', 'u', 'user', gift_value=999, gift_count=9, chat_count=8, like_count=7)
        result = self.svc.get_user_messages('room-1', user_id='u', session_id=live.id)
        self.assertEqual(result['stats'], {'total_messages': 0, 'chat_count': 0, 'gift_count': 0, 'like_count': 0, 'total_value': 0})
        self.assertEqual(result['pagination']['total'], 0)

    def test_date_range_includes_last_microsecond(self):
        self.svc.create_live_room('room-1', anchor_name='test')
        self.svc.create_live_session('room-1', start_time=datetime(2026, 11, 21, 23, 59, 59, 999999, tzinfo=CHINA_TZ), status='ended', total_income=5)
        self.assertEqual(len(self.svc.get_room_sessions_stats('room-1', end_date='2026-11-21')), 1)
        self.assertEqual(self.svc.get_sessions_aggregated_stats('room-1', end_date='2026-11-21')['total_income'], 5)


class StatsDashboardMarkupTests(unittest.TestCase):
    def test_stats_page_exposes_reworked_dashboard_sections(self):
        with open('templates/stats.html', encoding='utf-8') as f:
            html = f.read()
        with open('static/js/stats.js', encoding='utf-8') as f:
            script = f.read()

        for marker in (
            'data-dashboard-section="core-metrics"',
            'data-dashboard-section="trend-chart"',
            'data-dashboard-section="detail-grid"',
            'data-metric-card="total-income"',
            'data-metric-card="total-sessions"',
            'data-metric-card="total-gifts"',
            'data-metric-card="total-likes"',
            'table-panel-header',
            'empty-panel',
            '/static/vendor/vue.min.js',
            '/static/vendor/tailwind.min.css',
            '/static/vendor/echarts.min.js',
            'ref="trendChart"',
        ):
            self.assertIn(marker, html)
        self.assertNotIn('cdn.jsdelivr.net', html)

        self.assertIn("chartMetric: 'income'", script)
        self.assertIn('chartHasSignal()', script)
        self.assertIn('normalizeChartMetric()', script)
        self.assertIn('renderTrendChart()', script)
        self.assertIn('buildTrendChartOption()', script)
        self.assertIn('formatAvgLikes()', script)
        self.assertIn('this._trendChart.getDom()', script)
        self.assertIn('this._trendChart.dispose();', script)
        self.assertIn('mergedRelatedRooms()', script)
        self.assertIn('canAutoLoadSelectedRoomStats()', script)
        self.assertIn('autoLoadSelectedRoomStats()', script)
        self.assertIn('this.autoLoadSelectedRoomStats();', script)
        self.assertIn('formatDurationParts(seconds)', script)
        self.assertIn('@change="autoLoadSelectedRoomStats"', html)
        self.assertIn('v-text="formatAvgLikes()"', html)
        # 场均卡片需保留 tooltip 说明分母语义
        self.assertIn('未产生收入的场次不计入分母', html)
        self.assertIn('零点赞场次不计入分母', html)
        # 用户卡片：点赞作为只读 chip，不是 tab
        self.assertIn('user-stat-chips', html)
        self.assertIn('data-chip="like"', html)
        self.assertNotIn('user-tab-button flex-1 cursor-default', html)


if __name__ == '__main__':
    unittest.main()
