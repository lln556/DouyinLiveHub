import threading
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from models.database import CHINA_TZ
from services import room_manager as room_manager_module
from services.data_service import DataService
from services.scheduler_service import SchedulerService
from ws_handlers.handlers import WebDouyinLiveFetcher


class FakeDataService:
    def __init__(self):
        self.room = SimpleNamespace(
            live_id="123",
            status="monitoring",
            archived_at=None,
            auto_reconnect=False,
        )
        self.updated = []
        self.status_updates = []
        self.events = []

    def get_live_room(self, live_id):
        return self.room if live_id == self.room.live_id else None

    def update_live_room(self, live_id, **kwargs):
        self.updated.append((live_id, kwargs))
        for key, value in kwargs.items():
            setattr(self.room, key, value)
        return True

    def update_live_room_status(self, live_id, status, error_message=None):
        self.status_updates.append((live_id, status, error_message))
        self.room.status = status
        return True

    def log_system_event(self, *args, **kwargs):
        self.events.append((args, kwargs))
        return True

    def get_current_live_session(self, live_id):
        return None


class RecordingContributionDataService(FakeDataService):
    def __init__(self):
        super().__init__()
        self.contribution_updates = []
        self.session_stat_updates = []

    def update_user_contribution(self, *args, **kwargs):
        self.contribution_updates.append((args, kwargs))
        return SimpleNamespace()

    def increment_session_stats(self, *args, **kwargs):
        self.session_stat_updates.append((args, kwargs))
        return True

    def get_current_live_session(self, live_id):
        if live_id == self.room.live_id:
            return SimpleNamespace(
                id=77,
                start_time=None,
                end_time=None,
                status="live",
                total_income=0,
                total_gift_count=0,
                total_chat_count=0,
                total_like_count=sum(
                    kwargs.get("like_count_delta", 0)
                    for _, kwargs in self.session_stat_updates
                ),
                peak_viewer_count=0,
            )
        return None


class FakeSocketIO:
    def __init__(self):
        self.emitted = []

    def emit(self, *args, **kwargs):
        self.emitted.append((args, kwargs))


class FakeStartedRoom:
    def __init__(self, live_id, manager, socketio=None):
        self.live_id = live_id
        self.manager = manager
        self.socketio = socketio
        self.shutdown_event = threading.Event()
        self.thread = None
        self.started = False

    def start(self):
        self.started = True


class SlowStoppingRoom:
    def __init__(self, started, release):
        self.started = started
        self.release = release

    def stop(self):
        self.started.set()
        self.release.wait(timeout=2)


class AliveThread:
    def is_alive(self):
        return True


class RunningRoom:
    def __init__(self):
        self.shutdown_event = threading.Event()
        self.thread = AliveThread()
        self.start_called = False

    def start(self):
        self.start_called = True


class RoomManagerLifecycleTest(unittest.TestCase):
    def make_manager(self, data_service=None):
        manager = room_manager_module.RoomManager.__new__(room_manager_module.RoomManager)
        manager.data_service = data_service or FakeDataService()
        manager.socketio = None
        manager.active_rooms = {}
        manager.lock = threading.Lock()
        return manager

    def test_shutdown_preserves_auto_reconnect_and_joins_monitor_thread(self):
        manager = self.make_manager()
        manager.data_service.room.auto_reconnect = True
        room = room_manager_module.MonitoredRoom("123", manager)
        manager.active_rooms["123"] = room
        room.thread = Mock()
        room.fetcher = Mock()
        with patch.object(room, "_schedule_manual_stop_session_check") as manual_check:
            manager.shutdown()
        self.assertTrue(room.shutdown_event.is_set())
        self.assertTrue(manager.data_service.room.auto_reconnect)
        self.assertEqual(manager.data_service.room.status, "stopped")
        self.assertEqual(manager.data_service.events, [])
        manual_check.assert_not_called()
        room.fetcher.stop.assert_called_once()
        room.thread.join.assert_called_once_with(timeout=3)

    def test_shutdown_preserves_database_auto_reconnect(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
            data_service = DataService(f"sqlite:///{db_file.name}")
            try:
                data_service.create_tables()
                data_service.create_live_room("123", anchor_name="anchor", auto_reconnect=True, monitor_type="24h")
                manager = self.make_manager(data_service)
                manager.active_rooms["123"] = room_manager_module.MonitoredRoom("123", manager)
                manager.shutdown()
                self.assertTrue(data_service.get_live_room("123").auto_reconnect)
            finally:
                data_service.close_session()

    def test_offline_without_active_session_is_checked_only_once_per_threshold(self):
        manager = self.make_manager()
        room = room_manager_module.MonitoredRoom("123", manager)
        with (
            patch.object(manager.data_service, "get_current_live_session", return_value=None) as query,
            patch.object(room_manager_module.config, "MONITOR_OFFLINE_END_THRESHOLD", 2),
        ):
            for _ in range(12):
                self.assertFalse(room.check_and_end_session_if_offline())
            query.assert_called_once()
            room.reset_offline_counter()
            for _ in range(2):
                room.check_and_end_session_if_offline()
            self.assertEqual(query.call_count, 2)

    def test_cookie_update_performs_io_after_releasing_manager_lock(self):
        manager = self.make_manager()
        room = room_manager_module.MonitoredRoom("123", manager)
        room.thread = AliveThread()

        def update(cookie, reconnect):
            acquired = manager.lock.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                manager.lock.release()

        room.fetcher = SimpleNamespace(
            update_douyin_cookie=Mock(side_effect=update),
            _fetcher=SimpleNamespace(ws=object()),
        )
        manager.data_service.update_live_room_reconnect = Mock()
        manager.active_rooms["123"] = room
        self.assertEqual(manager.update_douyin_cookie("sessionid=new"), 1)
        room.fetcher.update_douyin_cookie.assert_called_once_with("sessionid=new", reconnect=True)

    def test_cookie_update_does_not_mark_static_waiting_room_for_reconnect(self):
        manager = self.make_manager()
        room = room_manager_module.MonitoredRoom("123", manager)
        room.fetcher = SimpleNamespace(
            update_douyin_cookie=Mock(), _fetcher=SimpleNamespace(ws=None))
        manager.active_rooms["123"] = room
        self.assertEqual(manager.update_douyin_cookie("sessionid=new"), 1)
        self.assertFalse(room.cookie_refresh_requested)
        room.fetcher.update_douyin_cookie.assert_called_once_with("sessionid=new", reconnect=False)

    def test_cookie_update_does_not_reconnect_closed_socket_while_polling(self):
        manager = self.make_manager()
        room = room_manager_module.MonitoredRoom("123", manager)
        room.thread = AliveThread()
        room.fetcher = SimpleNamespace(update_douyin_cookie=Mock(),
                                       _fetcher=SimpleNamespace(ws=SimpleNamespace(keep_running=False)))
        manager.active_rooms["123"] = room
        manager.data_service.update_live_room_reconnect = Mock()
        manager.update_douyin_cookie("sessionid=new")
        self.assertFalse(room.cookie_refresh_requested)
        manager.data_service.update_live_room_reconnect.assert_not_called()
        room.fetcher.update_douyin_cookie.assert_called_once_with("sessionid=new", reconnect=False)

    def test_all_room_status_uses_snapshot_and_queries_outside_lock(self):
        manager = self.make_manager()
        room = room_manager_module.MonitoredRoom("123", manager)
        manager.active_rooms["123"] = room
        manager.data_service.room.anchor_name = "anchor"
        manager.data_service.room.monitor_type = "24h"
        manager.data_service.room.reconnect_count = 0

        def list_rooms():
            acquired = manager.lock.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                manager.active_rooms.clear()
                manager.lock.release()
            return [manager.data_service.room]

        manager.data_service.list_live_rooms = Mock(side_effect=list_rooms)
        result = manager.get_all_rooms_status()
        self.assertEqual([row["live_id"] for row in result], ["123"])
        manager.data_service.list_live_rooms.assert_called_once()

    def test_scheduler_uses_snapshot_when_rooms_removed_during_stats_save(self):
        manager = self.make_manager()
        room = room_manager_module.MonitoredRoom("123", manager)
        manager.active_rooms.update({"123": room, "456": room})
        saved = []

        def save(live_id, **kwargs):
            with manager.lock:
                manager.active_rooms.pop("456", None)
            saved.append(live_id)

        manager.data_service.get_live_room = lambda live_id: SimpleNamespace(status="monitoring")
        manager.data_service.save_room_stats = save
        scheduler = SchedulerService(manager, manager.data_service)
        scheduler._save_stats_snapshot()
        self.assertEqual(saved, ["123", "456"])

    def test_dedupe_caches_are_bounded_and_reset_in_place(self):
        room = room_manager_module.MonitoredRoom("123", self.make_manager())
        trace_cache = room.trace_dedupe
        with room.data_lock:
            for index in range(2500):
                self.assertTrue(room.remember_trace(str(index)))
                self.assertTrue(room.remember_like(str(index)))
            self.assertFalse(room.remember_trace("2499"))
            self.assertFalse(room.remember_like("2499"))
            self.assertEqual(len(room.trace_dedupe), 2000)
            self.assertEqual(len(room.trace_dedupe_set), 2000)
            self.assertEqual(len(room.like_dedupe_set), 2000)
            self.assertTrue(room.remember_trace("0"))
            room.gift_user_ids.add("user")
            room.reset_dedupe()
        self.assertIs(trace_cache, room.trace_dedupe)
        self.assertFalse(room.trace_dedupe or room.trace_dedupe_set or room.like_dedupe)
        self.assertFalse(room.gift_user_ids)

    def test_start_can_continue_while_previous_stop_is_still_returning(self):
        manager = self.make_manager()
        stop_started = threading.Event()
        release_stop = threading.Event()
        manager.active_rooms["123"] = SlowStoppingRoom(stop_started, release_stop)

        stop_done = threading.Event()
        start_done = threading.Event()

        def stop_room():
            self.assertTrue(manager.stop_room("123"))
            stop_done.set()

        def start_room():
            self.assertTrue(manager.start_room("123"))
            start_done.set()

        with (
            patch.object(room_manager_module, "MonitoredRoom", FakeStartedRoom),
            patch.object(room_manager_module.config, "ANTI_DETECTION_ENABLED", False),
        ):
            stopper = threading.Thread(target=stop_room)
            starter = threading.Thread(target=start_room)
            stopper.start()
            self.assertTrue(stop_started.wait(timeout=1))

            starter.start()
            self.assertTrue(start_done.wait(timeout=1))

            release_stop.set()
            stopper.join(timeout=1)
            starter.join(timeout=1)

        self.assertTrue(stop_done.is_set())
        self.assertIsInstance(manager.active_rooms["123"], FakeStartedRoom)
        self.assertTrue(manager.active_rooms["123"].started)

    def test_superseded_stop_does_not_overwrite_restarted_room_state(self):
        data_service = FakeDataService()
        manager = self.make_manager(data_service)

        old_room = room_manager_module.MonitoredRoom("123", manager)
        manager.active_rooms["123"] = object()

        old_room.stop()

        self.assertEqual(data_service.updated, [])
        self.assertEqual(data_service.status_updates, [])
        self.assertEqual(data_service.events, [])

    def test_start_existing_running_room_does_not_reset_status_to_offline(self):
        data_service = FakeDataService()
        manager = self.make_manager(data_service)
        running_room = RunningRoom()
        manager.active_rooms["123"] = running_room

        self.assertTrue(manager.start_room("123"))

        self.assertFalse(running_room.start_called)
        self.assertEqual(data_service.status_updates, [])
        self.assertEqual(data_service.updated, [("123", {"auto_reconnect": True})])

    def test_monitored_room_start_is_thread_safe(self):
        manager = self.make_manager()
        room = room_manager_module.MonitoredRoom("123", manager)
        loop_started = 0
        loop_lock = threading.Lock()
        release_loop = threading.Event()

        def fake_monitor_loop(self):
            nonlocal loop_started
            with loop_lock:
                loop_started += 1
            release_loop.wait(timeout=2)

        with patch.object(room_manager_module.MonitoredRoom, "_monitor_loop", fake_monitor_loop):
            callers = [threading.Thread(target=room.start) for _ in range(8)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(timeout=1)

            self.assertEqual(loop_started, 1)

            room.shutdown_event.set()
            release_loop.set()
            room.thread.join(timeout=1)

    def test_offline_status_must_cross_threshold_before_ending_session(self):
        manager = self.make_manager()
        room = room_manager_module.MonitoredRoom("123", manager)
        ended_reasons = []

        class FakeFetcher:
            current_session_id = 77

            def _end_current_session(self, reason):
                ended_reasons.append(reason)
                return True

        room.fetcher = FakeFetcher()

        with patch.object(room_manager_module.config, "MONITOR_OFFLINE_END_THRESHOLD", 3):
            self.assertFalse(room.check_and_end_session_if_offline("first false"))
            self.assertFalse(room.check_and_end_session_if_offline("second false"))
            self.assertTrue(room.check_and_end_session_if_offline("third false"))

        self.assertEqual(len(ended_reasons), 1)
        self.assertIn("连续3次未开播", ended_reasons[0])

    def test_create_live_session_reuses_existing_live_session(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
            data_service = DataService(f"sqlite:///{db_file.name}")
            try:
                data_service.create_tables()
                data_service.create_live_room(
                    "123",
                    anchor_name="anchor",
                    monitor_type="24h",
                    auto_reconnect=True,
                    status="monitoring"
                )

                first = data_service.create_live_session("123", anchor_name="anchor", status="live")
                second = data_service.create_live_session("123", anchor_name="anchor", status="live")
                sessions = data_service.get_live_sessions("123", status="live", limit=10)

                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                self.assertEqual(first.id, second.id)
                self.assertEqual(len(sessions), 1)
            finally:
                data_service.close_session()

    def test_search_users_by_name_respects_live_session_scope(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
            data_service = DataService(f"sqlite:///{db_file.name}")
            try:
                data_service.create_tables()
                data_service.create_live_room(
                    "123",
                    anchor_name="anchor",
                    monitor_type="24h",
                    auto_reconnect=True,
                    status="monitoring"
                )
                first_session = data_service.create_live_session(
                    "123",
                    anchor_name="anchor",
                    status="ended",
                    start_time=datetime(2026, 1, 1, 0, 0, tzinfo=CHINA_TZ),
                    end_time=datetime(2026, 1, 1, 1, 0, tzinfo=CHINA_TZ),
                )
                second_session = data_service.create_live_session(
                    "123",
                    anchor_name="anchor",
                    status="live",
                    start_time=datetime(2026, 1, 2, 0, 0, tzinfo=CHINA_TZ),
                )

                data_service.save_gift_message(
                    "123",
                    live_session_id=first_session.id,
                    anchor_name="anchor",
                    user_id="user-old",
                    user_name="同名用户",
                    gift_name="旧礼物",
                    gift_count=1,
                    gift_price=100,
                    total_value=100,
                )
                data_service.save_gift_message(
                    "123",
                    live_session_id=second_session.id,
                    anchor_name="anchor",
                    user_id="user-current",
                    user_name="同名用户",
                    gift_name="新礼物",
                    gift_count=1,
                    gift_price=5,
                    total_value=5,
                )

                current_results = data_service.search_users_by_name(
                    live_id="123",
                    user_name="同名",
                    session_id=second_session.id,
                    limit=30,
                )
                room_results = data_service.search_users_by_name(
                    live_id="123",
                    user_name="同名",
                    limit=30,
                )

                self.assertEqual([user["user_id"] for user in current_results], ["user-current"])
                self.assertEqual(current_results[0]["total_value"], 5)
                self.assertEqual(current_results[0]["session_id"], second_session.id)
                self.assertEqual([user["user_id"] for user in room_results], ["user-old", "user-current"])
            finally:
                data_service.close_session()

    def test_date_range_contributors_merge_renamed_user_by_id(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
            data_service = DataService(f"sqlite:///{db_file.name}")
            try:
                data_service.create_tables()
                data_service.create_live_room(
                    "123",
                    anchor_name="anchor",
                    monitor_type="24h",
                    auto_reconnect=True,
                    status="monitoring"
                )

                data_service.save_gift_message(
                    "123",
                    anchor_name="anchor",
                    user_id="renamed-user",
                    user_name="张九麟",
                    gift_name="旧礼物",
                    gift_count=1,
                    gift_price=100,
                    total_value=100,
                    created_at=datetime(2026, 1, 1, 0, 0, tzinfo=CHINA_TZ),
                )
                data_service.save_chat_message(
                    "123",
                    anchor_name="anchor",
                    user_id="renamed-user",
                    user_name="张九麟",
                    content="旧弹幕",
                    created_at=datetime(2026, 1, 1, 0, 1, tzinfo=CHINA_TZ),
                )
                data_service.save_gift_message(
                    "123",
                    anchor_name="anchor",
                    user_id="renamed-user",
                    user_name="麟",
                    gift_name="新礼物",
                    gift_count=1,
                    gift_price=50,
                    total_value=50,
                    created_at=datetime(2026, 1, 2, 0, 0, tzinfo=CHINA_TZ),
                )
                data_service.save_chat_message(
                    "123",
                    anchor_name="anchor",
                    user_id="renamed-user",
                    user_name="麟",
                    content="新弹幕",
                    created_at=datetime(2026, 1, 2, 0, 1, tzinfo=CHINA_TZ),
                )

                result = data_service.get_contributors_by_date_range(
                    live_id="123",
                    start_date="2026-01-01",
                    end_date="2026-01-02",
                    page=1,
                    page_size=20,
                )

                self.assertEqual(result["total"], 1)
                self.assertEqual(len(result["contributors"]), 1)
                contributor = result["contributors"][0]
                self.assertEqual(contributor["user_id"], "renamed-user")
                self.assertEqual(contributor["nickname"], "麟")
                self.assertEqual(contributor["contribution_value"], 150)
                self.assertEqual(contributor["gift_count"], 2)
                self.assertEqual(contributor["chat_count"], 2)
            finally:
                data_service.close_session()

    def test_summary_contributors_use_user_contribution_totals(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
            data_service = DataService(f"sqlite:///{db_file.name}")
            try:
                data_service.create_tables()
                data_service.create_live_room(
                    "123",
                    anchor_name="anchor",
                    monitor_type="24h",
                    auto_reconnect=True,
                    status="monitoring"
                )

                data_service.update_user_contribution(
                    "123",
                    "anchor",
                    "summary-user",
                    "麟",
                    gift_value=74620,
                    gift_count=308,
                    chat_count=18,
                    fans_club_level=12,
                )
                data_service.save_gift_message(
                    "123",
                    anchor_name="anchor",
                    user_id="summary-user",
                    user_name="麟",
                    gift_name="新礼物",
                    gift_count=1,
                    gift_price=899,
                    total_value=899,
                    created_at=datetime(2026, 5, 11, 10, 30, tzinfo=CHINA_TZ),
                )

                result = data_service.get_summary_contributors(
                    live_id="123",
                    page=1,
                    page_size=20,
                )

                self.assertEqual(result["total"], 1)
                contributor = result["contributors"][0]
                self.assertEqual(contributor["user_id"], "summary-user")
                self.assertEqual(contributor["nickname"], "麟")
                self.assertEqual(contributor["contribution_value"], 74620)
                self.assertEqual(contributor["gift_count"], 308)
                self.assertEqual(contributor["chat_count"], 18)
                self.assertEqual(contributor["fans_club_level"], 12)
            finally:
                data_service.close_session()

    def test_like_counts_are_tracked_on_user_session_and_aggregate_stats(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
            data_service = DataService(f"sqlite:///{db_file.name}")
            try:
                data_service.create_tables()
                data_service.create_live_room(
                    "123",
                    anchor_name="anchor",
                    monitor_type="24h",
                    auto_reconnect=True,
                    status="monitoring"
                )
                live_session = data_service.create_live_session("123", anchor_name="anchor", status="live")

                data_service.update_user_contribution(
                    "123",
                    "anchor",
                    "like-user",
                    "点赞用户",
                    like_count=7,
                )
                data_service.increment_session_stats(live_session.id, like_count_delta=7)

                user = data_service.get_user_contribution("123", "like-user")
                session_stats = data_service.get_live_session_stats(live_session.id)
                aggregate = data_service.get_sessions_aggregated_stats("123")
                user_messages = data_service.get_user_messages(
                    live_id="123",
                    user_id="like-user",
                    message_type="all",
                )

                self.assertEqual(user.like_count, 7)
                self.assertEqual(session_stats["total_like_count"], 7)
                self.assertEqual(aggregate["total_like_count"], 7)
                self.assertEqual(user_messages["stats"]["like_count"], 7)
            finally:
                data_service.close_session()

    def test_summary_contributors_enrich_chat_count_and_user_level_from_messages(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
            data_service = DataService(f"sqlite:///{db_file.name}")
            try:
                data_service.create_tables()
                data_service.create_live_room(
                    "123",
                    anchor_name="anchor",
                    monitor_type="24h",
                    auto_reconnect=True,
                    status="monitoring"
                )

                data_service.update_user_contribution(
                    "123",
                    "anchor",
                    "summary-user",
                    "麟",
                    gift_value=74620,
                    gift_count=308,
                    chat_count=0,
                )
                data_service.save_chat_message(
                    "123",
                    anchor_name="anchor",
                    user_id="summary-user",
                    user_name="麟",
                    user_level=35,
                    content="第一条弹幕",
                    created_at=datetime(2026, 5, 11, 10, 29, tzinfo=CHINA_TZ),
                )
                data_service.save_chat_message(
                    "123",
                    anchor_name="anchor",
                    user_id="summary-user",
                    user_name="麟",
                    user_level=34,
                    content="第二条弹幕",
                    created_at=datetime(2026, 5, 11, 10, 30, tzinfo=CHINA_TZ),
                )
                data_service.save_gift_message(
                    "123",
                    anchor_name="anchor",
                    user_id="summary-user",
                    user_name="麟",
                    user_level=33,
                    gift_name="礼物",
                    gift_count=1,
                    gift_price=899,
                    total_value=899,
                    created_at=datetime(2026, 5, 11, 10, 31, tzinfo=CHINA_TZ),
                )

                result = data_service.get_summary_contributors(
                    live_id="123",
                    page=1,
                    page_size=20,
                )

                contributor = result["contributors"][0]
                self.assertEqual(contributor["chat_count"], 2)
                self.assertEqual(contributor["user_level"], 35)
            finally:
                data_service.close_session()

    def test_monitored_room_persists_chat_count_and_gift_count_to_contribution_summary(self):
        data_service = RecordingContributionDataService()
        manager = self.make_manager(data_service)
        room = room_manager_module.MonitoredRoom("123", manager)

        room.update_contribution(
            "summary-user",
            "麟",
            gift_value=100,
            gift_count=3,
            chat_count=2,
            user_level=35,
        )

        self.assertEqual(len(data_service.contribution_updates), 1)
        args, kwargs = data_service.contribution_updates[0]
        self.assertEqual(args[:4], ("123", None, "summary-user", "麟"))
        self.assertEqual(kwargs["gift_value"], 100)
        self.assertEqual(kwargs["gift_count"], 3)
        self.assertEqual(kwargs["chat_count"], 2)

    def test_monitored_room_persists_like_count_to_contribution_summary(self):
        data_service = RecordingContributionDataService()
        manager = self.make_manager(data_service)
        room = room_manager_module.MonitoredRoom("123", manager)

        room.update_contribution(
            "like-user",
            "点赞用户",
            like_count=9,
            user_level=20,
        )

        self.assertEqual(room.user_contributions["like-user"]["like_count"], 9)
        args, kwargs = data_service.contribution_updates[0]
        self.assertEqual(args[:4], ("123", None, "like-user", "点赞用户"))
        self.assertEqual(kwargs["like_count"], 9)

    def test_like_message_handler_deduplicates_by_message_id(self):
        data_service = RecordingContributionDataService()
        manager = self.make_manager(data_service)
        room = room_manager_module.MonitoredRoom("123", manager)
        # 模拟监控期间已建立点赞基线：之前某条消息 total=100
        room.stats['like_baseline_total'] = 100
        room.stats['like_last_seen_total'] = 100
        socketio = FakeSocketIO()
        fetcher = WebDouyinLiveFetcher.__new__(WebDouyinLiveFetcher)
        fetcher.live_id = "123"
        fetcher.monitored_room = room
        fetcher.socketio = socketio
        fetcher.current_session_id = 77
        fetcher.like_message_keys = []
        fetcher.log = SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
        )

        like_msg = SimpleNamespace(
            common=SimpleNamespace(msg_id=9988),
            count=5,
            total=120,
            user=SimpleNamespace(
                nick_name="点赞用户",
                id=42,
                id_str="user-42",
                pay_grade=SimpleNamespace(level=18),
                fans_club=None,
            ),
            double_like_detail=SimpleNamespace(seq_id=1),
        )

        fetcher._handle_like_message(like_msg)
        fetcher._handle_like_message(like_msg)

        # 监控期间增量 = total - last_seen = 120 - 100 = 20
        self.assertEqual(room.stats["total_like_count"], 20)
        self.assertEqual(room.stats["like_last_seen_total"], 120)
        # user_contributions 仍按消息 count 归属
        self.assertEqual(room.user_contributions["user-42"]["like_count"], 5)
        self.assertEqual(len(data_service.contribution_updates), 1)
        # session 入账用 total 差值，而不是 count
        self.assertEqual(len(data_service.session_stat_updates), 1)
        self.assertEqual(data_service.session_stat_updates[0][1]["like_count_delta"], 20)
        self.assertEqual(len(socketio.emitted), 1)

    def test_user_messages_falls_back_to_contribution_name_when_messages_are_pruned(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
            data_service = DataService(f"sqlite:///{db_file.name}")
            try:
                data_service.create_tables()
                data_service.create_live_room(
                    "123",
                    anchor_name="anchor",
                    monitor_type="24h",
                    auto_reconnect=True,
                    status="monitoring"
                )

                data_service.update_user_contribution(
                    "123",
                    "anchor",
                    "summary-only-user",
                    "🌈146斤的王晶🌈",
                    gift_value=12000,
                    gift_count=2,
                    chat_count=0,
                    fans_club_level=0,
                )

                result = data_service.get_user_messages(
                    live_id="123",
                    user_id="summary-only-user",
                    message_type="all",
                    limit=50,
                    offset=0,
                )

                self.assertEqual(result["user"]["user_id"], "summary-only-user")
                self.assertEqual(result["user"]["nickname"], "🌈146斤的王晶🌈")
                self.assertEqual(result["stats"]["total_value"], 12000)
                self.assertEqual(result["stats"]["gift_count"], 2)
                self.assertEqual(result["stats"]["chat_count"], 0)
                self.assertEqual(result["messages"], [])
            finally:
                data_service.close_session()


@pytest.mark.parametrize("status", [True, False, None, "exception"])
def test_poll_always_closes_temp_fetcher_and_waits_interruptibly(monkeypatch, status):
    manager = RoomManagerLifecycleTest().make_manager()
    room = room_manager_module.MonitoredRoom("123", manager)
    temp = SimpleNamespace(close=Mock(), status_error_message="代理连接异常")
    temp.anchor_info = {"anchor_name": "anchor", "anchor_id": "42"}

    def get_status():
        if status == "exception":
            raise RuntimeError("network failed")
        return status

    temp.get_room_status = get_status
    monkeypatch.setattr("crawler.DouyinLiveWebFetcher", lambda *a: temp)
    waits = []

    def wait(interval):
        waits.append(interval)
        room.shutdown_event.set()
        return True

    monkeypatch.setattr(room.shutdown_event, "wait", wait)
    # 旧实现用循环 sleep，避免回归测试等上完整轮询间隔。
    monkeypatch.setattr(room_manager_module.time, "sleep", lambda *a: room.shutdown_event.set())
    assert room._poll_room_status() is (status is True)
    temp.close.assert_called_once()
    if status is True:
        assert (manager.data_service.room.anchor_name, manager.data_service.room.anchor_id) == ("anchor", "42")
    else:
        assert len(waits) == 1
    if status is None:
        assert manager.data_service.status_updates[-1][2] == "代理连接异常"


@pytest.mark.parametrize("status", [False, None, True])
def test_monitor_records_connection_time_only_for_live_room_and_closes_session(monkeypatch, status):
    manager = RoomManagerLifecycleTest().make_manager()
    room = room_manager_module.MonitoredRoom("123", manager)
    core = SimpleNamespace(close=Mock())
    fetcher = SimpleNamespace(
        _fetcher=core, get_room_status=lambda: status,
        start=Mock(side_effect=room.shutdown_event.set), current_session_id=None)
    monkeypatch.setattr("ws_handlers.handlers.WebDouyinLiveFetcher", lambda *a: fetcher)

    def poll(initial_confirmed_offline=False):
        assert initial_confirmed_offline is (status is False)
        room.shutdown_event.set()
        return False

    monkeypatch.setattr(room, "_poll_room_status", poll)
    room._monitor_loop()
    assert bool(room.last_connect_time) is (status is True)
    assert any("last_connect_time" in values for _, values in manager.data_service.updated) is (status is True)
    core.close.assert_called_once()


@pytest.mark.parametrize("initial_confirmed_offline", [False, True])
def test_poll_debounces_empty_status_after_confirmed_offline(monkeypatch, initial_confirmed_offline):
    manager = RoomManagerLifecycleTest().make_manager()
    room = room_manager_module.MonitoredRoom("123", manager)
    waiting = "主播未开播，等待开播中..."
    error = "直播间接口返回异常，等待重试..."
    statuses = ([False] if not initial_confirmed_offline else []) + [None, None, None, False, None, None]
    expected = ([waiting] if not initial_confirmed_offline else []) + [waiting, waiting, error, waiting, waiting, waiting]
    probes = iter(statuses)
    fetchers = []

    def create(*args):
        fetcher = SimpleNamespace(get_room_status=lambda: next(probes),
                                  status_error_message=error, close=Mock())
        fetchers.append(fetcher)
        return fetcher

    def wait(interval):
        if len(fetchers) == len(statuses):
            room.shutdown_event.set()
            return True
        return False

    monkeypatch.setattr("crawler.DouyinLiveWebFetcher", create)
    monkeypatch.setattr(room.shutdown_event, "wait", wait)
    assert room._poll_room_status(initial_confirmed_offline=initial_confirmed_offline) is False
    assert [update[2] for update in manager.data_service.status_updates] == expected
    assert all(update[1] == "offline" for update in manager.data_service.status_updates)
    assert all(fetcher.close.call_count == 1 for fetcher in fetchers)


def test_status_probe_throttle_disabled_does_not_sleep_or_read_clock(monkeypatch):
    from crawler import fetcher

    monkeypatch.setattr(fetcher.config, "ANTI_DETECTION_ENABLED", False)
    sleep = Mock(side_effect=AssertionError("disabled throttle must not sleep"))
    clock = Mock(side_effect=AssertionError("disabled throttle must not read clock"))
    monkeypatch.setattr(fetcher.time, "sleep", sleep)
    monkeypatch.setattr(fetcher.time, "monotonic", clock)
    fetcher._throttle_status_probe()
    sleep.assert_not_called()
    clock.assert_not_called()


def test_status_probe_throttle_spaces_concurrent_rooms_without_real_sleep(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from crawler import fetcher

    monkeypatch.setattr(fetcher.config, "ANTI_DETECTION_ENABLED", True)
    monkeypatch.setattr(fetcher, "_LAST_STATUS_PROBE_AT", 0.0)
    clock = [10.0]
    sleeps = []

    def sleep(delay):
        assert fetcher._STATUS_PROBE_LOCK.locked()
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(fetcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fetcher.time, "sleep", sleep)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: fetcher._throttle_status_probe(), range(4)))
    assert sleeps == [1.5, 1.5, 1.5]
    assert fetcher._LAST_STATUS_PROBE_AT == 14.5


def test_reconnect_uses_interruptible_backoff_with_jitter(monkeypatch):
    manager = RoomManagerLifecycleTest().make_manager()
    manager.data_service.room.auto_reconnect = True
    manager.data_service.update_live_room_reconnect = Mock()
    room = room_manager_module.MonitoredRoom("123", manager)
    delays = []

    def wait(delay):
        delays.append(delay)
        if len(delays) == 2:
            room.shutdown_event.set()
            return True
        return False

    monkeypatch.setattr(room.shutdown_event, "wait", wait)
    monkeypatch.setattr(room_manager_module.config, "MONITOR_RECONNECT_DELAY", 10)
    monkeypatch.setattr(room_manager_module.config, "MONITOR_MAX_RETRIES", 5)
    monkeypatch.setattr(room_manager_module, "apply_jitter", lambda delay: delay + 1)
    monkeypatch.setattr(room_manager_module.time, "sleep", lambda *a: room.shutdown_event.set())
    fetchers = []

    def create(*args):
        fetcher = SimpleNamespace(
            _fetcher=SimpleNamespace(close=Mock()), get_room_status=lambda: True,
            start=Mock(), current_session_id=77)
        fetchers.append(fetcher)
        return fetcher

    monkeypatch.setattr("ws_handlers.handlers.WebDouyinLiveFetcher", create)
    room._monitor_loop()
    assert delays == [11, 21]
    assert len(fetchers) == 2
    assert all(fetcher._fetcher.close.call_count == 1 for fetcher in fetchers)


@pytest.mark.parametrize("display", ["manager", "terminal"])
def test_display_uses_snapshot_taken_before_database_io(display):
    from utils.status_display import StatusDisplay

    manager = RoomManagerLifecycleTest().make_manager()
    room = room_manager_module.MonitoredRoom("123", manager)
    room.update_stats(current_user_count=7, total_income=12)
    manager.active_rooms["123"] = room
    manager.data_service.room.anchor_name = "anchor"
    manager.data_service.room.error_message = None

    def list_rooms():
        with manager.lock:
            manager.active_rooms.clear()
        return [manager.data_service.room]

    manager.data_service.list_live_rooms = list_rooms
    if display == "manager":
        rows = manager.get_display_status()
    else:
        rows = StatusDisplay(manager)._get_display_data()
    assert rows[0]["current_user_count"] == 7
    assert rows[0]["total_income"] == 12


def test_rich_display_leaves_first_run_state(monkeypatch):
    from utils import status_display

    display = status_display.StatusDisplay(RoomManagerLifecycleTest().make_manager())
    display._rich_mode = True
    monkeypatch.setattr(display, "_build_table", Mock())
    live = Mock()
    context = Mock(__enter__=Mock(return_value=live), __exit__=Mock(return_value=False))
    monkeypatch.setattr(status_display, "Live", lambda *a, **kw: context)
    monkeypatch.setattr(display._stop_event, "wait", lambda *a: display._stop_event.set())
    display._run()
    live.update.assert_called_once()
    assert display._first_run is False


def test_error_log_rotates_by_size_and_time_and_cleans_expired_logs(tmp_path):
    import os
    import time
    from datetime import timedelta
    from utils.logger import _ErrorLogRotation, logger

    # 小文件低流量场景也必须触发按天 retention 清理。
    expired = tmp_path / "error_2000-01-01.log"
    expired.write_text("old error")
    old_time = time.time() - 10 * 86400
    os.utime(expired, (old_time, old_time))
    rotation = _ErrorLogRotation("1 GB")
    sink = logger.add(tmp_path / "error_{time:YYYY-MM-DD}.log", rotation=rotation, retention="1 day")
    try:
        logger.debug("first error")
        logger.patch(lambda record: record.update(time=record["time"] + timedelta(days=1))).debug("next day")
        assert not expired.exists()
    finally:
        logger.remove(sink)

    with (tmp_path / "size.log").open("w+") as file:
        file.write("123456")
        class LogMessage(str):
            pass

        message = LogMessage("next")
        message.record = {"time": datetime.now().astimezone()}
        assert _ErrorLogRotation("5 B")(message, file) is True


def test_dedupe_survives_monitor_restart_and_handles_concurrent_replay(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    room = room_manager_module.MonitoredRoom("123", RoomManagerLifecycleTest().make_manager())
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(room.remember_trace, ["same trace"] * 100)) == 1
    room.remember_like("like")
    room.gift_user_ids.add("user")
    monkeypatch.setattr(room, "_monitor_loop", lambda: None)
    room.start()
    room.thread.join(timeout=1)
    assert room.remember_trace("same trace") is False
    assert room.remember_like("like") is False
    assert room.gift_user_ids == {"user"}


def test_daily_backup_registration_is_deferred_and_replaceable():
    manager = RoomManagerLifecycleTest().make_manager()
    service = SchedulerService(manager, manager.data_service)
    backup_calls = []

    def backup():
        backup_calls.append(True)

    service.scheduler.start(paused=True)
    try:
        service.register_daily_backup(backup)
        job = service.register_daily_backup(backup)
        assert [item.id for item in service.get_jobs()] == ['daily_database_backup']
        assert "hour='4'" in str(job.trigger)
        assert "minute='30'" in str(job.trigger)
        assert backup_calls == []
    finally:
        service.scheduler.shutdown()


@pytest.mark.parametrize("ended", [False, True])
def test_disconnect_without_session_only_skips_backoff_for_confirmed_end(monkeypatch, ended):
    manager = RoomManagerLifecycleTest().make_manager()
    manager.data_service.room.auto_reconnect = True
    manager.data_service.update_live_room_reconnect = Mock()
    room = room_manager_module.MonitoredRoom("123", manager)

    def start():
        if ended:
            manager.data_service.update_live_room_status("123", "stopped")

    fetcher = SimpleNamespace(_fetcher=SimpleNamespace(close=Mock()),
                              get_room_status=lambda: True, start=start, current_session_id=None)
    monkeypatch.setattr("ws_handlers.handlers.WebDouyinLiveFetcher", lambda *a: fetcher)
    poll = Mock(side_effect=lambda: (room.shutdown_event.set(), False)[1])
    monkeypatch.setattr(room, "_poll_room_status", poll)
    wait = Mock(side_effect=lambda *a: (room.shutdown_event.set(), True)[1])
    monkeypatch.setattr(room.shutdown_event, "wait", wait)
    room._monitor_loop()
    assert poll.call_count == int(ended)
    assert wait.call_count == int(not ended)


if __name__ == "__main__":
    unittest.main()
