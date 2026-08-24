"""Local HTTP server for watching live outputs and browsing the archive.

Why HTTP instead of pointing VLC at the file: on Windows a player holds
the file open, which blocks the atomic os.replace() that keeps
last_hour.mp4 current -- the refresh's short retry window loses against a
~60s playback pass. Serving over HTTP severs that tie: each request reads
the file fully into memory and closes it within milliseconds, so the
replace is never blocked, and VLC looping the URL re-requests it on every
pass -- each loop plays the newest hour.

Beyond the two live outputs, the server also serves each camera's
archived sessions/ videos and renders minimal index pages ("/" lists
cameras, "/live/<camera>/" lists that camera's files) -- so a browser on
another device is a full remote viewer for everything the host machine
holds. Live outputs keep the read-whole-into-memory approach (bounded at
~26 MB, and it sidesteps the atomic-replace race); archived files are
streamed from disk in blocks instead -- they're immutable once renamed
into sessions/ (no replace race) and a 6-hour block can reach hundreds
of MB, the wrong size to buffer per request.

Bind address is platform-dependent by default: loopback-only on Windows
(nothing exposed to the network, no firewall prompt -- there's always a
local screen to watch from), LAN-visible on Linux (a headless Pi has no
screen of its own, so watching the feed means watching it from another
device on the network). Either can be overridden via Config's
stream_bind_host. There is no authentication -- fine on a trusted home
LAN, never port-forward this. Remote access belongs behind an
authenticating tunnel (e.g. Cloudflare Tunnel + Access, see README),
never a raw router port-forward.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote, unquote

from .config import app_root_dir

STREAM_PORT = 8177
_ALLOWED_FILES = ("last_hour.mp4", "session.mp4")
_STREAM_BLOCK = 65536

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


def _live_root() -> Path:
    return app_root_dir() / "Timelapses" / "Live"


def _valid_segment(name: str) -> bool:
    """One URL path segment that must map to a single file/dir name --
    never a traversal. Segments arrive %-decoded, so separators can
    reappear here even though the URL was split on '/'."""
    return not ("/" in name or "\\" in name or name in ("", ".", ".."))


def _page(title: str, body_html: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:sans-serif;margin:1.5em;max-width:44em}"
        "a{text-decoration:none} li{margin:.35em 0} .m{color:#777;font-size:.85em}"
        "</style></head><body>" + body_html + "</body></html>"
    ).encode("utf-8")


def _root_index() -> bytes:
    cameras = sorted((d.name for d in _live_root().iterdir() if d.is_dir())
                     if _live_root().is_dir() else [])
    items = "".join(
        f"<li><a href='/live/{quote(c)}/'>{html.escape(c)}</a></li>" for c in cameras
    ) or "<li class='m'>(no cameras have live folders yet)</li>"
    return _page("Reolink Timelapse", f"<h2>Cameras</h2><ul>{items}</ul>")


def _camera_index(camera: str) -> Optional[bytes]:
    cam_dir = _live_root() / camera
    if not _valid_segment(camera) or not cam_dir.is_dir():
        return None

    def entry(href: str, label: str, path: Path) -> str:
        try:
            st = path.stat()
            meta = (f"{st.st_size / 1e6:.1f} MB &middot; "
                    f"{dt.datetime.fromtimestamp(st.st_mtime):%b %d %H:%M}")
        except OSError:
            return ""
        return (f"<li><a href='{href}'>{html.escape(label)}</a> "
                f"<span class='m'>{meta}</span></li>")

    live_items = "".join(
        entry(f"/live/{quote(camera)}/{name}", name, cam_dir / name)
        for name in _ALLOWED_FILES if (cam_dir / name).exists()
    ) or "<li class='m'>(not live right now)</li>"

    sessions_dir = cam_dir / "sessions"
    archived: List[Path] = sorted(
        sessions_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True
    ) if sessions_dir.is_dir() else []
    archive_items = "".join(
        entry(f"/live/{quote(camera)}/sessions/{quote(p.name)}", p.name, p)
        for p in archived
    ) or "<li class='m'>(no archived sessions yet)</li>"

    return _page(
        camera,
        f"<p><a href='/'>&larr; cameras</a></p><h2>{html.escape(camera)}</h2>"
        f"<h3>Live</h3><ul>{live_items}</ul>"
        f"<h3>Archived sessions</h3><ul>{archive_items}</ul>",
    )


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass  # VLC re-requests every loop pass; per-request logging is noise

    def _load(self, camera: str, filename: str) -> Optional[bytes]:
        """Read a live output whole (see module docstring for why whole).

        Returns None (after sending the error) on any problem.
        """
        path = _live_root() / camera / filename
        if not _valid_segment(camera) or not path.parent.is_dir():
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

    def _range_bounds(self, total: int) -> Optional[Tuple[int, int, int]]:
        """(status, start, end) honouring a single-range Range header, or
        None after sending 416 for an unsatisfiable one."""
        start, end, status = 0, total - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "") or "!")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), end)
            else:  # suffix range: last N bytes
                start = max(0, total - int(m.group(2)))
            if start > end or start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return None
            status = 206
        return status, start, end

    def _send_video_headers(self, status: int, start: int, end: int, total: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()

    def _respond(self, data: bytes, head_only: bool) -> None:
        bounds = self._range_bounds(len(data))
        if bounds is None:
            return
        status, start, end = bounds
        self._send_video_headers(status, start, end, len(data))
        if not head_only:
            self.wfile.write(data[start:end + 1])

    def _respond_archived(self, camera: str, filename: str, head_only: bool) -> None:
        """Stream an archived sessions/ video from disk. Immutable files,
        so no replace race -- and far too large to buffer per request."""
        path = _live_root() / camera / "sessions" / filename
        if not (_valid_segment(camera) and _valid_segment(filename)
                and filename.endswith(".mp4") and path.is_file()):
            self.send_error(404)
            return
        try:
            total = path.stat().st_size
            f = open(path, "rb")
        except OSError:
            self.send_error(404)
            return
        with f:
            bounds = self._range_bounds(total)
            if bounds is None:
                return
            status, start, end = bounds
            self._send_video_headers(status, start, end, total)
            if head_only:
                return
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                block = f.read(min(_STREAM_BLOCK, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def _send_html(self, page: Optional[bytes], head_only: bool) -> None:
        if page is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        if not head_only:
            self.wfile.write(page)

    def _handle(self, head_only: bool) -> None:
        try:
            raw = self.path.split("?", 1)[0]
            parts = [unquote(p) for p in raw.strip("/").split("/")] if raw.strip("/") else []
            if not parts:
                self._send_html(_root_index(), head_only)
            elif len(parts) == 2 and parts[0] == "live":
                self._send_html(_camera_index(parts[1]), head_only)
            elif len(parts) == 3 and parts[0] == "live" and parts[2] in _ALLOWED_FILES:
                data = self._load(parts[1], parts[2])
                if data is not None:
                    self._respond(data, head_only)
            elif len(parts) == 4 and parts[0] == "live" and parts[2] == "sessions":
                self._respond_archived(parts[1], parts[3], head_only)
            else:
                self.send_error(404)
        except (ConnectionError, OSError):
            pass  # player closed the connection mid-transfer; normal

    def do_GET(self) -> None:
        self._handle(head_only=False)

    def do_HEAD(self) -> None:
        self._handle(head_only=True)


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
