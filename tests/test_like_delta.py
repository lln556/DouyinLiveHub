"""单测 WebDouyinLiveFetcher._apply_like_delta 的「监控期间增量」计算。"""

import unittest

from ws_handlers.handlers import WebDouyinLiveFetcher


def _fresh_stats() -> dict:
    return {
        'current_user_count': 0,
        'total_user_count': 0,
        'total_like_count': 0,
        'like_baseline_total': None,
        'like_last_seen_total': None,
        'total_income': 0,
        'contributor_count': 0,
    }


class ApplyLikeDeltaTests(unittest.TestCase):
    def test_first_message_with_total_establishes_baseline_and_returns_zero(self):
        stats = _fresh_stats()

        delta = WebDouyinLiveFetcher._apply_like_delta(stats, count=3, total=100)

        self.assertEqual(delta, 0)
        self.assertEqual(stats['like_baseline_total'], 100)
        self.assertEqual(stats['like_last_seen_total'], 100)
        self.assertEqual(stats['total_like_count'], 0)

    def test_subsequent_message_returns_total_diff_not_message_count(self):
        stats = _fresh_stats()
        WebDouyinLiveFetcher._apply_like_delta(stats, count=3, total=100)

        delta = WebDouyinLiveFetcher._apply_like_delta(stats, count=5, total=150)

        self.assertEqual(delta, 50)
        self.assertEqual(stats['like_last_seen_total'], 150)
        self.assertEqual(stats['total_like_count'], 50)

    def test_zero_count_but_total_growth_still_counts(self):
        """关键场景：抖音校准型消息 count=0、total 涨，旧实现会丢，新实现要计入。"""
        stats = _fresh_stats()
        WebDouyinLiveFetcher._apply_like_delta(stats, count=1, total=200)

        delta = WebDouyinLiveFetcher._apply_like_delta(stats, count=0, total=350)

        self.assertEqual(delta, 150)
        self.assertEqual(stats['total_like_count'], 150)

    def test_total_regression_returns_zero_and_keeps_last_seen(self):
        stats = _fresh_stats()
        WebDouyinLiveFetcher._apply_like_delta(stats, count=0, total=500)
        WebDouyinLiveFetcher._apply_like_delta(stats, count=0, total=700)

        delta = WebDouyinLiveFetcher._apply_like_delta(stats, count=0, total=600)

        self.assertEqual(delta, 0)
        self.assertEqual(stats['like_last_seen_total'], 700)
        self.assertEqual(stats['total_like_count'], 200)

    def test_fallback_to_count_when_total_missing(self):
        stats = _fresh_stats()

        delta = WebDouyinLiveFetcher._apply_like_delta(stats, count=4, total=0)

        self.assertEqual(delta, 4)
        self.assertEqual(stats['total_like_count'], 4)
        self.assertIsNone(stats['like_baseline_total'])

    def test_noop_when_both_count_and_total_are_zero(self):
        stats = _fresh_stats()
        WebDouyinLiveFetcher._apply_like_delta(stats, count=2, total=100)
        snapshot = dict(stats)

        delta = WebDouyinLiveFetcher._apply_like_delta(stats, count=0, total=0)

        self.assertEqual(delta, 0)
        self.assertEqual(stats, snapshot)

    def test_accumulates_across_many_messages(self):
        stats = _fresh_stats()
        # 首条建立基线 1000
        WebDouyinLiveFetcher._apply_like_delta(stats, count=10, total=1000)

        increments = [
            (5, 1020),    # +20
            (0, 1080),    # +60（count=0 也要算）
            (3, 1100),    # +20
            (0, 1100),    # +0（重复 total）
            (8, 1150),    # +50
        ]
        deltas = [
            WebDouyinLiveFetcher._apply_like_delta(stats, count=c, total=t)
            for c, t in increments
        ]

        self.assertEqual(deltas, [20, 60, 20, 0, 50])
        self.assertEqual(sum(deltas), 150)
        self.assertEqual(stats['total_like_count'], 150)  # 1150 - 1000
        self.assertEqual(stats['like_last_seen_total'], 1150)


if __name__ == "__main__":
    unittest.main()
