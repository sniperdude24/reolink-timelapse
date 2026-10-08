"""GET /login for someone already logged in redirects instead of 404ing."""

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from reolink_timelapse import webstream


@pytest.fixture
def server():
    # Loopback without Cloudflare headers counts as a logged-in admin.
    srv = ThreadingHTTPServer(("127.0.0.1", 0), webstream._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp


def test_logged_in_get_login_redirects_home(server):
    resp = _get(server, "/login")
    assert resp.status == 303
    assert resp.getheader("Location") == "/"


def test_logged_in_get_login_honours_safe_next(server):
    assert _get(server, "/login?next=/live/Backyard/").getheader("Location") == "/live/Backyard/"
    # Never an off-site redirect.
    assert _get(server, "/login?next=//evil.example").getheader("Location") == "/"


def test_logged_out_get_login_still_shows_form(server, monkeypatch):
    monkeypatch.setattr(webstream, "_auth_enabled", lambda: True)
    resp = _get(server, "/login", {"CF-Connecting-IP": "203.0.113.9"})
    assert resp.status == 401
