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
        self.assertEqual(stats['avg_income'], 200)
        self.assertEqual(stats['avg_like_count'], 1000)


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
            'chart-zero-state',
            'table-panel-header',
            'empty-panel',
            'https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js',
            'ref="trendChart"',
        ):
            self.assertIn(marker, html)

        self.assertIn("chartMetric: 'income'", script)
        self.assertIn('chartHasSignal()', script)
        self.assertIn('normalizeChartMetric()', script)
        self.assertIn('renderTrendChart()', script)
        self.assertIn('buildTrendChartOption()', script)
        self.assertIn('formatAvgLikes()', script)
        self.assertIn('this._trendChart.getDom()', script)
        self.assertIn('this._trendChart.dispose();', script)
        self.assertIn('canAutoLoadSelectedRoomStats()', script)
        self.assertIn('autoLoadSelectedRoomStats()', script)
        self.assertIn('this.autoLoadSelectedRoomStats();', script)
        self.assertIn('trendChartPoints()', script)
        self.assertIn('formatDurationParts(seconds)', script)
        self.assertIn('@change="autoLoadSelectedRoomStats"', html)
        self.assertIn('v-text="formatAvgLikes()"', html)


if __name__ == '__main__':
    unittest.main()
