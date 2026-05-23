import threading
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from models.database import CHINA_TZ
from services import room_manager as room_manager_module
from services.data_service import DataService


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

    def update_user_contribution(self, *args, **kwargs):
        self.contribution_updates.append((args, kwargs))
        return SimpleNamespace()


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


if __name__ == "__main__":
    unittest.main()
