"""Local HTTP server for watching live outputs in an external player.

Why HTTP instead of pointing VLC at the file: on Windows a player holds
the file open, which blocks the atomic os.replace() that keeps
last_hour.mp4 current -- the refresh's short retry window loses against a
~60s playback pass. Serving over HTTP severs that tie: each request reads
the file fully into memory and closes it within milliseconds, so the
replace is never blocked, and VLC looping the URL re-requests it on every
pass -- each loop plays the newest hour.

Localhost-only by design: nothing is exposed to the network and no
firewall prompt appears. The whole file fits comfortably in memory
(a last-hour window is ~26 MB of 1080p segments).
"""

from __future__ import annotations

import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import quote, unquote

from .config import app_root_dir

STREAM_HOST = "127.0.0.1"
STREAM_PORT = 8177
_ALLOWED_FILES = ("last_hour.mp4", "session.mp4")

_server: Optional[ThreadingHTTPServer] = None
_server_lock = threading.Lock()


def stream_url(camera_name: str, filename: str = "last_hour.mp4") -> str:
    return f"http://{STREAM_HOST}:{STREAM_PORT}/live/{quote(camera_name)}/{filename}"


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
            self._respond(data, head_only=True)


def start_stream_server(log: Callable[[str], None] = print) -> None:
    """Start the server on a daemon thread. Idempotent; a busy port is
    logged and tolerated -- the app must keep working without the server."""
    global _server
    with _server_lock:
        if _server is not None:
            return
        try:
            server = ThreadingHTTPServer((STREAM_HOST, STREAM_PORT), _Handler)
        except OSError as e:
            log(f"Live stream server not started (port {STREAM_PORT}): {e}")
            return
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _server = server
