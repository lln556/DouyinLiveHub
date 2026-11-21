"""签名、PushFrame 与直播状态判定的离线回归测试。"""
import gzip
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from crawler import fetcher as module
from crawler.signature import get__ac_signature
from protobuf.douyin import HeadersList, Message, PushFrame, Response


@pytest.fixture
def fetcher(monkeypatch):
    monkeypatch.setattr(module.config, "ANTI_DETECTION_ENABLED", False)
    instance = module.DouyinLiveWebFetcher("123", douyin_cookie="ttwid=test")
    instance._DouyinLiveWebFetcher__room_id = "456"
    monkeypatch.setattr(instance, "get_ac_nonce", lambda: "nonce")
    monkeypatch.setattr(instance, "get_a_bogus", lambda params: "ab")
    yield instance
    instance.session.close()


def test_signature_uses_time_at_each_call(monkeypatch):
    monkeypatch.setattr("crawler.signature.time.time", lambda: 1_700_000_000)
    first = get__ac_signature("www.douyin.com/", "nonce", "ua")
    assert first == get__ac_signature("www.douyin.com/", "nonce", "ua", 1_700_000_000)
    monkeypatch.setattr("crawler.signature.time.time", lambda: 1_700_000_060)
    second = get__ac_signature("www.douyin.com/", "nonce", "ua")
    assert second == get__ac_signature("www.douyin.com/", "nonce", "ua", 1_700_000_060)
    assert first != second


def test_signature_accepts_missing_nonce():
    assert get__ac_signature("www.douyin.com/", None, "ua", 123) == get__ac_signature(
        "www.douyin.com/", "", "ua", 123)


@pytest.mark.parametrize("compressed,header", [(False, False), (True, False), (True, True)])
def test_push_frame_decompression(compressed, header):
    payload = bytes(Response(cursor="cursor"))
    package = PushFrame(payload=gzip.compress(payload) if compressed else payload)
    if header:
        package.headers_list = [HeadersList(key="compress_type", value="gzip")]
    assert module.decompress_push_frame_payload(package) == payload


def test_gzip_header_is_respected_even_without_magic(monkeypatch):
    package = PushFrame(payload=b"encoded", headers_list=[HeadersList(key="compress_type", value="gzip")])
    decompress = Mock(return_value=b"decoded")
    monkeypatch.setattr(module.gzip, "decompress", decompress)
    assert module.decompress_push_frame_payload(package) == b"decoded"
    decompress.assert_called_once_with(b"encoded")


def test_empty_push_frame_skips_decompression():
    assert module.decompress_push_frame_payload(
        PushFrame(headers_list=[HeadersList(key="compress_type", value="gzip")])) == b""


@pytest.mark.parametrize("compressed", [False, True])
def test_core_message_handler_parses_payload_and_ack(fetcher, compressed):
    response = Response(need_ack=True, internal_ext="ack", messages_list=[
        Message(method="WebcastChatMessage", payload=b"chat")])
    payload = bytes(response)
    package = PushFrame(log_id=7, payload=gzip.compress(payload) if compressed else payload)
    fetcher._parseChatMsg = Mock()
    ws = Mock()
    fetcher._wsOnMessage(ws, bytes(package))
    fetcher._parseChatMsg.assert_called_once_with(b"chat")
    ack = PushFrame().parse(ws.send.call_args.args[0])
    assert (ack.log_id, ack.payload_type, ack.payload) == (7, "ack", b"ack")


def test_empty_message_handler_does_not_parse_response(fetcher, monkeypatch):
    response = Mock()
    monkeypatch.setattr(module, "Response", response)
    fetcher._wsOnMessage(Mock(), bytes(PushFrame(payload_type="hb")))
    response.assert_not_called()


@pytest.mark.parametrize("status,text", [
    (502, '{"data":{"room_status":2}}'), (403, '{"data":{"room_status":2}}'),
    (200, ""), (200, "   "), (200, "<html>blocked</html>"),
    (200, "null"), (200, "[]"), (200, "{}"), (200, '{"data":{}}'),
    (200, '{"data":{"room_status":null}}'),
    (200, '{"data":{"room_status":"invalid"}}'),
])
def test_room_status_unknown_responses_are_not_offline(fetcher, monkeypatch, status, text):
    response = requests.Response()
    response.status_code = status
    response._content = text.encode()
    monkeypatch.setattr(fetcher.session, "get", lambda *a, **kw: response)
    assert fetcher.get_room_status() is None
    assert fetcher.status_error_message


@pytest.mark.parametrize("status,expected", [(0, True), (2, False), (1, False)])
def test_explicit_room_status(fetcher, monkeypatch, status, expected):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({"data": {"room_status": status}}).encode()
    monkeypatch.setattr(fetcher.session, "get", lambda *a, **kw: response)
    assert fetcher.get_room_status() is expected


@pytest.mark.parametrize("html,expected", [
    ('{"viewer":{"id":"999","nickname":"viewer"},"owner":{"nickname":"anchor","id_str":"42"}}',
     {"anchor_name": "anchor", "anchor_id": "42"}),
    ('{"owner":{"avatar":{"id":"999"},"id":42,"nickname":"anchor"}}',
     {"anchor_name": "anchor", "anchor_id": "42"}),
    ('{\\"owner\\":{\\"nickname\\":\\"anchor\\",\\"id_str\\":\\"42\\"}}',
     {"anchor_name": "anchor", "anchor_id": "42"}),
    ('{"owner":{},"viewer":{"id":"999","nickname":"viewer"}}',
     {"anchor_name": None, "anchor_id": None}),
    ('{"viewer":{"id":"999","nickname":"viewer"}}',
     {"anchor_name": None, "anchor_id": None}),
])
def test_html_anchor_only_uses_owner(fetcher, monkeypatch, html, expected):
    response = requests.Response()
    response.status_code = 200
    response._content = html.encode()
    monkeypatch.setattr(fetcher.session, "get", lambda *a, **kw: response)
    assert fetcher._get_anchor_info_from_html() == expected


def test_fetcher_stop_closes_socket_and_websocket(fetcher):
    fetcher.stop()  # 未启动也能停止
    fetcher.ws = Mock()
    sock = fetcher.ws.sock
    fetcher.stop()
    sock.close.assert_called_once()
    fetcher.ws.close.assert_called_once()


def test_fetcher_close_releases_http_session(fetcher):
    fetcher.session = Mock()
    fetcher.close()
    fetcher.session.close.assert_called_once()


def test_js_context_reused_per_thread_and_script():
    path = module.JS_DIR + "/sign.js"
    first = module.execute_js(path)
    assert module.execute_js(path) is first
    assert module.execute_js(module.JS_DIR + "/a_bogus.js") is not first
    barrier = threading.Barrier(2)

    def worker():
        ctx = module.execute_js(path)
        barrier.wait(timeout=5)
        assert module.execute_js(path) is ctx
        return ctx

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        contexts = [future.result(timeout=10) for future in futures]
    assert contexts[0] is not contexts[1]
    assert all(ctx is not first for ctx in contexts)


def test_js_sources_are_cached_without_node_or_reopening(fetcher, monkeypatch):
    monkeypatch.setattr("builtins.open", Mock(side_effect=AssertionError("JS source reread")))
    monkeypatch.setattr("subprocess.Popen", Mock(side_effect=AssertionError("node subprocess")))
    # 绕过 fixture 的 a_bogus stub，运行真正的 V8 签名代码。
    assert module.DouyinLiveWebFetcher.get_a_bogus(fetcher, {"aid": "6383"})
    assert module.generateSignature("wss://example/?aid=6383&room_id=123")
    assert module.generateSignature("wss://example/?aid=6383&room_id=123")


@pytest.mark.parametrize("result", [None, ""])
def test_empty_websocket_signature_raises(monkeypatch, result):
    monkeypatch.setattr(module, "execute_js", lambda path: SimpleNamespace(call=lambda *a: result))
    with pytest.raises(ValueError, match="签名|signature"):
        module.generateSignature("wss://example/?aid=6383")


def test_failed_signature_invalidates_context(tmp_path):
    script = tmp_path / "sign.js"
    script.write_text('function get_sign(value) { throw new Error("broken"); }')
    first = module.execute_js(str(script))
    with pytest.raises(Exception, match="broken"):
        module.generateSignature("wss://example/?aid=6383", str(script))
    assert module.execute_js(str(script)) is not first


def test_concurrent_multithread_v8_signing_stress(fetcher):
    """多线程并发初始化与高频签名 + GC 不应触发 V8/PartitionAlloc 崩溃。"""
    import gc

    def worker(tid):
        for seq in range(10):
            sig = module.generateSignature(f"wss://example/?aid=6383&room_id={tid}_{seq}")
            ab = module.DouyinLiveWebFetcher.get_a_bogus(fetcher, {"aid": "6383", "room_id": f"{tid}_{seq}"})
            assert sig and ab

    for _ in range(4):
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(worker, tid) for tid in range(8)]
            for future in futures:
                future.result(timeout=15)
        gc.collect()
