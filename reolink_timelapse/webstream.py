"""Local HTTP server for watching live outputs in an external player.

Why HTTP instead of pointing VLC at the file: on Windows a player holds
the file open, which blocks the atomic os.replace() that keeps
last_hour.mp4 current -- the refresh's short retry window loses against a
~60s playback pass. Serving over HTTP severs that tie: each request reads
the file fully into memory and closes it within milliseconds, so the
replace is never blocked, and VLC looping the URL re-requests it on every
pass -- each loop plays the newest hour.

Bind address is platform-dependent by default: loopback-only on Windows
(nothing exposed to the network, no firewall prompt -- there's always a
local screen to watch from), LAN-visible on Linux (a headless Pi has no
screen of its own, so watching the feed means watching it from another
device on the network). Either can be overridden via Config's
stream_bind_host. There is no authentication -- fine on a trusted home
LAN, never port-forward this. The whole file fits comfortably in memory
(a last-hour window is ~26 MB of 1080p segments).
"""

from __future__ import annotations

import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import quote, unquote

from .config import app_root_dir

STREAM_PORT = 8177
_ALLOWED_FILES = ("last_hour.mp4", "session.mp4")

_server: Optional[ThreadingHTTPServer] = None
_server_lock = threading.Lock()
_bind_host: str = "127.0.0.1"  # updated by start_stream_server to whatever it actually bound


def default_bind_host() -> str:
    """Loopback on Windows (a screen is always local); LAN-visible on
    everything else, since a headless Pi has no local screen to watch
    from -- the feed is only useful from another device on the network."""
    return "127.0.0.1" if sys.platform == "win32" else "0.0.0.0"


def _detect_lan_ip() -> Optional[str]:
    """This machine's LAN-facing address, for turning a 0.0.0.0 bind into
    a URL someone can actually type into another device's VLC. The
    connect() below sends no packets (UDP, no handshake) -- it just asks
    the OS which local interface would be used to reach that address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def stream_url(camera_name: str, filename: str = "last_hour.mp4") -> str:
    host = _bind_host
    if host == "0.0.0.0":
        host = _detect_lan_ip() or "127.0.0.1"
    return f"http://{host}:{STREAM_PORT}/live/{quote(camera_name)}/{filename}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass  # VLC re-requests every loop pass; per-request logging is noise

    def _load(self) -> Optional[bytes]:
        """Resolve the request path to a live output and read it whole.

        Returns None (after sending the error) unless the path is exactly
        /live/<known camera dir>/<one of the two outputs>.
        """
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 3 or parts[0] != "live" or parts[2] not in _ALLOWED_FILES:
            self.send_error(404)
            return None
        camera = unquote(parts[1])
        live_root = app_root_dir() / "Timelapses" / "Live"
        path = live_root / camera / parts[2]
        # The camera segment must name a directory directly under Live/,
        # not a path that escapes it.
        if ("/" in camera or "\\" in camera or camera in (".", "..")
                or not path.parent.is_dir()):
            self.send_error(404)
            return None
        # Opening the file can hit a momentary sharing violation if it
        # lands exactly during the atomic replace that refreshes the
        # output; measured at ~1 in 400 requests under load. Retry briefly
        # so a player's loop pass never errors on that race.
        for attempt in range(5):
            try:
                with open(path, "rb") as f:
                    return f.read()
            except FileNotFoundError:
                self.send_error(404, "That live view has no video yet")
                return None
            except OSError:
                if attempt == 4:
                    self.send_error(500)
                    return None
                time.sleep(0.1)
        return None

    def _respond(self, data: bytes, head_only: bool) -> None:
        start, end = 0, len(data) - 1
        status = 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "") or "!")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), end)
            else:  # suffix range: last N bytes
                start = max(0, len(data) - int(m.group(2)))
            if start > end or start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        if not head_only:
            self.wfile.write(data[start:end + 1])

    def do_GET(self) -> None:
        data = self._load()
        if data is not None:
            try:
                self._respond(data, head_only=False)
            except (ConnectionError, OSError):
                pass  # player closed the connection mid-transfer; normal

    def do_HEAD(self) -> None:
        data = self._load()
        if data is not None:
            try:
                self._respond(data, head_only=True)
            except (ConnectionError, OSError):
                pass


def start_stream_server(host: Optional[str] = None, log: Callable[[str], None] = print) -> None:
    """Start the server on a daemon thread. Idempotent; a busy port is
    logged and tolerated -- the app must keep working without the server.

    `host` overrides the platform default (see default_bind_host()) --
    pass a Config's stream_bind_host, or leave it as None to use the
    default. If the serving thread ever dies, that is logged and the
    dead server is forgotten, so the next call here brings it back -- the
    Watch in VLC button calls this before every launch for exactly that
    reason.
    """
    global _server, _bind_host
    with _server_lock:
        if _server is not None:
            return
        host = host or default_bind_host()
        try:
            server = ThreadingHTTPServer((host, STREAM_PORT), _Handler)
        except OSError as e:
            log(f"Live stream server not started (port {STREAM_PORT}): {e}")
            return
        server.daemon_threads = True
        _bind_host = host
        if host not in ("127.0.0.1", "localhost"):
            log(f"Live stream server listening on {host}:{STREAM_PORT} -- reachable "
                f"from other devices on your network. There is no password, so this "
                f"is fine on a trusted home LAN but must never be port-forwarded "
                f"or exposed to the internet.")
        threading.Thread(target=_serve, args=(server, log), daemon=True).start()
        _server = server


def _serve(server: ThreadingHTTPServer, log: Callable[[str], None]) -> None:
    global _server
    try:
        server.serve_forever()
    except Exception as e:
        log(f"Live stream server stopped unexpectedly: {e}")
    finally:
        try:
            server.server_close()
        except OSError:
            pass
        with _server_lock:
            if _server is server:
                _server = None
