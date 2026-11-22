"""Public static caching, asset fingerprints and origin gzip regression tests."""
import gzip
import hashlib
import os
import random
import re
from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import Response, render_template_string, send_from_directory
from sqlalchemy.engine import make_url


@pytest.fixture
def app_module(monkeypatch):
    import app as module

    assert make_url(module.config.DATABASE_URL).database == "douyin_live_test"
    monkeypatch.setitem(module.app.config, "TESTING", True)
    monkeypatch.setattr(module.config, "AUTH_REQUIRED", True)
    monkeypatch.setattr(module, "initialize_once_async", lambda: None)
    monkeypatch.setattr(module, "_asset_version_cache", {}, raising=False)
    monkeypatch.setattr(module, "_gzip_cache", {}, raising=False)
    return module


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


@pytest.fixture
def authed_client(client, app_module):
    with client.session_transaction() as sess:
        sess.permanent = True
        sess["authenticated"] = True
    assert client.get_cookie(app_module.app.config["SESSION_COOKIE_NAME"]) is not None
    return client


def _vary(response):
    return {item.strip().lower() for item in response.headers.get("Vary", "").split(",") if item.strip()}


@pytest.mark.parametrize("path", [
    "/static/vendor/tailwind.min.css?v=abc",
    "/static/js/room.js?v=abc",
    "/favicon.ico?v=2",
    "/level_img/test-icon.svg",
    "/fansclub_img/test-icon.svg",
])
def test_public_static_with_session_cookie_is_immutable(authed_client, app_module, monkeypatch, tmp_path, path):
    (tmp_path / "test-icon.svg").write_text("<svg/>")
    real_send = send_from_directory

    def send_icon(directory, filename, **kwargs):
        if directory in ("data/level_img", "data/fansclub_img"):
            directory = tmp_path
        return real_send(directory, filename, **kwargs)

    monkeypatch.setattr(app_module, "send_from_directory", send_icon)
    response = authed_client.get(path)
    assert response.status_code == 200
    assert "Cookie" in response.request.headers
    assert "cookie" not in _vary(response)
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_unversioned_favicon_has_daily_cache(authed_client):
    response = authed_client.get("/static/favicon.svg")
    assert response.status_code == 200
    assert "cookie" not in _vary(response)
    assert response.headers["Cache-Control"] == "public, max-age=86400"


@pytest.mark.parametrize("path", ["/static/vendor/vue.min.js", "/favicon.ico"])
def test_static_bypasses_authentication_and_initialization(client, app_module, monkeypatch, path):
    fail = Mock(side_effect=AssertionError("public static must not inspect session or initialize"))
    monkeypatch.setattr(app_module, "is_authenticated", fail)
    monkeypatch.setattr(app_module, "initialize_once_async", fail)
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_public_static_keeps_origin_protection(authed_client, method):
    response = authed_client.open("/static/favicon.svg", method=method, headers={"Origin": "http://localhost:9999"})
    assert response.status_code == 403
    assert "public" not in response.headers.get("Cache-Control", "")


def test_asset_v_is_content_md5_and_available_to_templates(app_module):
    expected = hashlib.md5((Path(app_module.app.static_folder) / "vendor/vue.min.js").read_bytes()).hexdigest()[:8]
    version = app_module.asset_v("vendor/vue.min.js")
    assert re.fullmatch(r"[0-9a-f]{8}", version)
    assert version == expected
    with app_module.app.test_request_context():
        assert render_template_string("{{ asset_v('vendor/vue.min.js') }}") == version


def test_asset_v_refreshes_after_same_size_file_change(app_module, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.app, "static_folder", str(tmp_path))
    asset = tmp_path / "test.js"
    asset.write_bytes(b"first")
    first = app_module.asset_v("test.js")
    mtime = asset.stat().st_mtime_ns
    asset.write_bytes(b"other")
    os.utime(asset, ns=(mtime + 1_000_000_000, mtime + 1_000_000_000))
    assert app_module.asset_v("test.js") == hashlib.md5(b"other").hexdigest()[:8]
    assert app_module.asset_v("test.js") != first


@pytest.mark.parametrize("path", ["../config.py", "../../.env", "missing.js", "."])
def test_asset_v_rejects_unsafe_or_missing_files(app_module, path):
    assert app_module.asset_v(path) == "1"


def test_asset_v_rejects_symlink_outside_static(app_module, monkeypatch, tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    outside = tmp_path / "outside.js"
    outside.write_bytes(b"private test fixture")
    (root / "escape.js").symlink_to(outside)
    monkeypatch.setattr(app_module.app, "static_folder", str(root))
    assert app_module.asset_v("escape.js") == "1"
    assert app_module.asset_v(str(outside)) == "1"


def test_asset_v_handles_filesystem_error(app_module, monkeypatch):
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=OSError("test read failure")))
    assert app_module.asset_v("vendor/vue.min.js") == "1"


@pytest.mark.parametrize("header,expected", [
    ("", False), ("br", False), ("gzip", True), ("GZIP", True),
    ("gzip;q=0", False), ("gzip;q=0.0, *;q=1", False),
    ("*;q=0", False), ("*;q=0.5", True),
    ("br, gzip;q=0.8", True), ("gzip;q=0.8, *;q=0", True),
    ("gzip; q = 0", False), ("gzip;q=bad", False),
    ("gzip;q=-1", False), ("gzip;q=2", False), ("gzip;q=nan", False),
])
def test_accepts_gzip_respects_explicit_weights(app_module, header, expected):
    assert app_module._accepts_gzip(header) is expected


@pytest.mark.parametrize("path", ["/api/rooms", "/login", "/static/vendor/vue.min.js"])
def test_gzip_round_trip(authed_client, factories, path):
    if path == "/api/rooms":
        factories.room(live_id="test_http_room", error_message="test response padding " * 40)
    plain = authed_client.get(path)
    encoded = authed_client.get(path, headers={"Accept-Encoding": "br, gzip;q=0.8"})
    assert plain.status_code == encoded.status_code == 200
    assert len(plain.data) >= 512
    assert "Content-Encoding" not in plain.headers
    assert encoded.headers["Content-Encoding"] == "gzip"
    assert "accept-encoding" in _vary(plain) & _vary(encoded)
    assert gzip.decompress(encoded.data) == plain.data
    assert int(encoded.headers["Content-Length"]) == len(encoded.data) < len(plain.data)
    if path.startswith("/static/"):
        assert not plain.headers["ETag"].startswith("W/")
        assert encoded.headers["ETag"] == "W/" + plain.headers["ETag"]
        assert "cookie" not in _vary(encoded)


@pytest.mark.parametrize("weak", [False, True])
def test_static_conditional_request_preserves_cache_and_vary(authed_client, weak):
    path = "/static/vendor/vue.min.js?v=abc"
    first = authed_client.get(path, headers={"Accept-Encoding": "gzip"})
    assert first.headers["Content-Encoding"] == "gzip"
    etag = first.headers["ETag"]
    assert etag.startswith('W/"')
    response = authed_client.get(path, headers={
        "Accept-Encoding": "gzip", "If-None-Match": etag if weak else etag[2:],
    })
    assert response.status_code == 304
    assert response.data == b""
    assert "Content-Encoding" not in response.headers
    assert "accept-encoding" in _vary(response)
    assert "cookie" not in _vary(response)
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("header", [None, "gzip;q=0", "br", "gzip;q=0, *;q=1"])
def test_gzip_not_applied_without_client_acceptance(authed_client, header):
    headers = {} if header is None else {"Accept-Encoding": header}
    response = authed_client.get("/static/vendor/vue.min.js", headers=headers)
    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers
    assert "accept-encoding" in _vary(response)


def test_head_has_negotiation_headers_without_gzip_body(authed_client):
    response = authed_client.head("/static/vendor/vue.min.js?v=abc", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.data == b""
    assert "Content-Encoding" not in response.headers
    assert "accept-encoding" in _vary(response)
    assert "cookie" not in _vary(response)
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_small_health_response_is_not_compressed(client):
    response = client.get("/healthz", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert len(response.data) < 512
    assert "Content-Encoding" not in response.headers
    assert "accept-encoding" in _vary(response)


def test_binary_static_is_not_gzipped(authed_client, app_module, monkeypatch, tmp_path):
    binary = b"\x89PNG\r\n\x1a\n" + b"\0" * 1024
    (tmp_path / "test.png").write_bytes(binary)
    monkeypatch.setattr(app_module.app, "static_folder", str(tmp_path))
    response = authed_client.get("/static/test.png?v=abc", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == binary
    assert "Content-Encoding" not in response.headers
    assert "accept-encoding" not in _vary(response)


@pytest.mark.parametrize("kind", ["stream", "encoded", "incompressible", "partial", "error", "unknown_length", "oversized"])
def test_compression_skips_unsafe_or_unhelpful_responses(client, app_module, monkeypatch, kind):
    raw = b"test payload " * 100
    if kind == "stream":
        response = Response(iter([raw]), mimetype="text/plain")
    elif kind == "incompressible":
        raw = random.Random(0).randbytes(512)
        assert len(gzip.compress(raw, compresslevel=6)) >= len(raw)
        response = Response(raw, mimetype="text/plain")
    else:
        response = Response(raw, mimetype="text/plain")
        if kind == "encoded":
            response.headers["Content-Encoding"] = "br"
        elif kind == "partial":
            response.status_code = 206
        elif kind == "error":
            response.status_code = 500
        else:
            response.direct_passthrough = True
            response.content_length = None if kind == "unknown_length" else 10 * 1024 * 1024 + 1
            monkeypatch.setattr(response, "get_data", Mock(side_effect=AssertionError("must not buffer")))
    monkeypatch.setitem(app_module.app.view_functions, "healthz", lambda: response)
    result = client.get("/healthz", headers={"Accept-Encoding": "gzip"})
    assert result.data == raw
    assert result.headers.get("Content-Encoding") == ("br" if kind == "encoded" else None)


def test_static_gzip_cache_reuses_compressed_bytes(authed_client, app_module, monkeypatch):
    compress = Mock(wraps=gzip.compress)
    monkeypatch.setattr(app_module.gzip, "compress", compress)
    first = authed_client.get("/static/vendor/vue.min.js?v=first", headers={"Accept-Encoding": "gzip"})
    second = authed_client.get("/static/vendor/vue.min.js?v=second", headers={"Accept-Encoding": "gzip"})
    assert first.headers["Content-Encoding"] == second.headers["Content-Encoding"] == "gzip"
    assert first.data == second.data
    assert compress.call_count == 1


def test_static_gzip_cache_is_bounded(authed_client, app_module, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.app, "static_folder", str(tmp_path))
    compress = Mock(wraps=gzip.compress)
    monkeypatch.setattr(app_module.gzip, "compress", compress)
    for number in range(65):
        (tmp_path / f"test-{number}.js").write_bytes(b"test payload " * 100)
        response = authed_client.get(f"/static/test-{number}.js", headers={"Accept-Encoding": "gzip"})
        assert response.headers["Content-Encoding"] == "gzip"
    assert compress.call_count == 65
    authed_client.get("/static/test-0.js", headers={"Accept-Encoding": "gzip"})
    assert compress.call_count == 66


def test_static_gzip_cache_refreshes_after_file_change(authed_client, app_module, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.app, "static_folder", str(tmp_path))
    asset = tmp_path / "test.js"
    asset.write_bytes(b"first " * 100)
    first = authed_client.get("/static/test.js", headers={"Accept-Encoding": "gzip"})
    mtime = asset.stat().st_mtime_ns
    asset.write_bytes(b"other " * 100)
    os.utime(asset, ns=(mtime + 1_000_000_000, mtime + 1_000_000_000))
    second = authed_client.get("/static/test.js", headers={"Accept-Encoding": "gzip"})
    assert first.headers["Content-Encoding"] == second.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(first.data) == b"first " * 100
    assert gzip.decompress(second.data) == b"other " * 100
