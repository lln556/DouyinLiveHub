import gzip
import unittest
from unittest.mock import patch

from crawler.fetcher import DouyinLiveWebFetcher
from protobuf.douyin import Message, PushFrame, Response
from ws_handlers.handlers import WebDouyinLiveFetcher as WebHandlerFetcher


class FakeSocket:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


class FakeWebSocket:
    def __init__(self):
        self.close_count = 0
        self.sock = FakeSocket()

    def close(self):
        self.close_count += 1


class FakeLog:
    def __init__(self):
        self.messages = []

    def warning(self, message):
        self.messages.append(("warning", message))

    def error(self, message):
        self.messages.append(("error", message))

    def debug(self, message):
        self.messages.append(("debug", message))


class RecordingCoreFetcher:
    def __init__(self):
        self.data_count = 0
        self.methods = []

    def record_websocket_data(self):
        self.data_count += 1

    def record_websocket_method(self, method):
        self.methods.append(method)


class WebSocketWatchdogTest(unittest.TestCase):
    def make_fetcher(self):
        fetcher = DouyinLiveWebFetcher("123")
        fetcher.log = FakeLog()
        fetcher.ws = FakeWebSocket()
        return fetcher

    def test_watchdog_closes_websocket_when_connect_timeout_expires(self):
        fetcher = self.make_fetcher()
        fetcher._reset_websocket_watchdog_state(started_at=100.0)

        with patch("config.WS_CONNECT_TIMEOUT", 60):
            closed = fetcher._check_websocket_watchdog(now=161.0)

        self.assertTrue(closed)
        self.assertEqual(fetcher.ws.close_count, 1)
        self.assertEqual(fetcher.ws.sock.close_count, 1)

    def test_watchdog_closes_websocket_after_data_silence_timeout(self):
        fetcher = self.make_fetcher()
        fetcher._reset_websocket_watchdog_state(started_at=100.0)
        fetcher.record_websocket_open(now=105.0)
        fetcher.record_websocket_data(now=120.0)

        with patch("config.WS_DATA_SILENCE_TIMEOUT", 60):
            closed = fetcher._check_websocket_watchdog(now=181.0)

        self.assertTrue(closed)
        self.assertEqual(fetcher.ws.close_count, 1)
        self.assertEqual(fetcher.ws.sock.close_count, 1)

    def test_business_watchdog_ignores_system_messages_when_enabled(self):
        fetcher = self.make_fetcher()
        fetcher._reset_websocket_watchdog_state(started_at=100.0)
        fetcher.record_websocket_open(now=100.0)
        fetcher.record_websocket_method("WebcastRoomStatsMessage", now=150.0)

        with (
            patch("config.WS_BUSINESS_WATCHDOG_ENABLED", True),
            patch("config.WS_BUSINESS_SILENCE_TIMEOUT", 60),
        ):
            closed = fetcher._check_websocket_watchdog(now=161.0)

        self.assertTrue(closed)

    def test_business_watchdog_accepts_interactive_messages_when_enabled(self):
        fetcher = self.make_fetcher()
        fetcher._reset_websocket_watchdog_state(started_at=100.0)
        fetcher.record_websocket_open(now=100.0)
        fetcher.record_websocket_method("WebcastRoomStatsMessage", now=150.0)
        fetcher.record_websocket_method("WebcastChatMessage", now=150.0)

        with (
            patch("config.WS_BUSINESS_WATCHDOG_ENABLED", True),
            patch("config.WS_BUSINESS_SILENCE_TIMEOUT", 60),
        ):
            closed = fetcher._check_websocket_watchdog(now=161.0)

        self.assertFalse(closed)
        self.assertEqual(fetcher.ws.close_count, 0)

    def test_web_handler_records_received_data_and_method_on_core_fetcher(self):
        handler = WebHandlerFetcher.__new__(WebHandlerFetcher)
        handler._fetcher = RecordingCoreFetcher()
        handler.log = FakeLog()

        response = Response(messages_list=[
            Message(method="WebcastChatMessage", payload=b"")
        ])
        frame = PushFrame(payload=gzip.compress(response.SerializeToString()))

        handler._wsOnMessage(FakeWebSocket(), frame.SerializeToString())

        self.assertEqual(handler._fetcher.data_count, 1)
        self.assertEqual(handler._fetcher.methods, ["WebcastChatMessage"])


if __name__ == "__main__":
    unittest.main()
