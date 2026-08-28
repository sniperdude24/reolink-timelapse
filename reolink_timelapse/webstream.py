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
stream_bind_host.

Authentication is an opt-in multi-user login. Accounts live in
webusers.py's store (stream_users.yaml, salted PBKDF2 hashes -- never
plaintext) and are managed from the web UI itself: admins get a /users
page to add users, reset PINs, and remove people (which logs their
devices out immediately), no config editing involved. With no accounts
anywhere the server stays open -- the original trusted-LAN posture --
and the legacy single-account config keys (stream.auth_user/auth_pin)
are imported once as the first admin. When enabled, every page and
video requires either a logged-in session cookie (the login form, for
browsers) or HTTP Basic credentials (for VLC:
http://user:pin@host:8177/...). Brute force is blunted with a global
lockout: 5 straight failures locks the login for 30s, doubling per
further failure up to 15 minutes. Requests from the same machine
(loopback) skip auth and count as an admin (also the recovery path if
every PIN is forgotten) -- EXCEPT when they carry Cloudflare headers,
because a Cloudflare Tunnel's cloudflared runs on this same host and
hands remote internet traffic to us *from* loopback; those requests
must authenticate like any other remote visitor. A login still isn't a
reason to port-forward this -- remote access belongs behind an HTTPS
tunnel (see README) so credentials aren't sent in the clear.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote

from .config import app_root_dir

STREAM_PORT = 8177
_ALLOWED_FILES = ("last_hour.mp4", "session.mp4")
_STREAM_BLOCK = 65536
_SESSION_SECONDS = 30 * 24 * 3600  # logged-in browsers stay logged in ~a month
_LOCKOUT_AFTER = 5      # straight failures before the login locks...
_LOCKOUT_BASE = 30.0    # ...for this many seconds, doubling per failure...
_LOCKOUT_MAX = 900.0    # ...capped here. 10,000 PINs at ~15 min each = months.

_server: Optional[ThreadingHTTPServer] = None
_server_lock = threading.Lock()
_bind_host: str = "127.0.0.1"  # updated by start_stream_server to whatever it actually bound

_store = None            # webusers.UserStore once auth is configured
_auth_lock = threading.Lock()
_sessions: dict = {}     # cookie token -> (username, expiry timestamp)
_fail_streak: int = 0
_locked_until: float = 0.0

_LOCAL_USER = "__local__"  # pseudo-identity for the host machine itself


def _auth_enabled() -> bool:
    return _store is not None and len(_store) > 0


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


def _root_index(identity: Tuple[str, bool]) -> bytes:
    cameras = sorted((d.name for d in _live_root().iterdir() if d.is_dir())
                     if _live_root().is_dir() else [])
    items = "".join(
        f"<li><a href='/live/{quote(c)}/'>{html.escape(c)}</a></li>" for c in cameras
    ) or "<li class='m'>(no cameras have live folders yet)</li>"
    footer = ""
    if identity[1]:
        footer += "<p class='m'><a href='/users'>manage users</a></p>"
    if _auth_enabled() and identity[0] not in ("", _LOCAL_USER):
        footer += (f"<p class='m'>logged in as {html.escape(identity[0])} &middot; "
                   f"<a href='/logout'>log out</a></p>")
    return _page("Reolink Timelapse", f"<h2>Cameras</h2><ul>{items}</ul>{footer}")


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


def _check_credentials(user: str, pin: str) -> Tuple[Optional[str], float]:
    """(matched_username_or_None, locked_for_seconds). Wrong credentials
    feed the global lockout; a correct login clears it. The user store's
    comparisons are constant-time (and hash-cost-constant even for
    unknown usernames) so response timing leaks nothing."""
    global _fail_streak, _locked_until
    if not _auth_enabled():
        return "", 0.0
    with _auth_lock:
        now = time.time()
        if now < _locked_until:
            return None, _locked_until - now
    rec = _store.verify(user, pin)  # slow hash: deliberately outside the lock
    with _auth_lock:
        if rec is not None:
            _fail_streak = 0
        else:
            _fail_streak += 1
            if _fail_streak >= _LOCKOUT_AFTER:
                _locked_until = time.time() + min(
                    _LOCKOUT_BASE * 2 ** (_fail_streak - _LOCKOUT_AFTER), _LOCKOUT_MAX)
    return (rec["name"] if rec else None), 0.0


def _new_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with _auth_lock:
        now = time.time()
        for tok in [t for t, (_, exp) in _sessions.items() if exp < now]:
            del _sessions[tok]
        _sessions[token] = (username, now + _SESSION_SECONDS)
    return token


def _session_user(token: str) -> Optional[str]:
    with _auth_lock:
        entry = _sessions.get(token)
        if entry is None or entry[1] < time.time():
            _sessions.pop(token, None)
            return None
        _sessions[token] = (entry[0], time.time() + _SESSION_SECONDS)  # sliding
        return entry[0]


def _drop_user_sessions(username: str) -> None:
    """A removed user's logged-in devices stop working immediately."""
    with _auth_lock:
        for tok in [t for t, (u, _) in _sessions.items()
                    if u.casefold() == username.casefold()]:
            del _sessions[tok]


def _safe_next(target: str) -> str:
    """Only ever redirect within this site (no open redirect)."""
    return target if target.startswith("/") and not target.startswith("//") else "/"


def _login_page(next_path: str, error: str = "") -> bytes:
    msg = f"<p style='color:#b00'>{html.escape(error)}</p>" if error else ""
    return _page(
        "Log in",
        f"<h2>Reolink Timelapse</h2>{msg}"
        f"<form method='post' action='/login'>"
        f"<input type='hidden' name='next' value='{html.escape(_safe_next(next_path), quote=True)}'>"
        f"<p><label>Username<br>"
        f"<input name='username' autocomplete='username' autofocus "
        f"style='font-size:1.2em;padding:.3em'></label></p>"
        f"<p><label>PIN<br>"
        f"<input name='pin' type='password' "
        f"autocomplete='current-password' style='font-size:1.2em;padding:.3em'></label></p>"
        f"<p><button style='font-size:1.1em;padding:.4em 1.5em'>Log in</button></p>"
        f"</form>",
    )


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass  # VLC re-requests every loop pass; per-request logging is noise

    def send_response(self, code: int, message: Optional[str] = None) -> None:
        """Every response is uncacheable. Found the hard way (2026-08-28):
        Cloudflare's edge caches .mp4 by default when the origin says
        nothing, which (a) served video to requests that never passed the
        login -- a removed user kept watching from cache -- and (b) served
        up to 4-hour-stale "last hour" video to remote viewers. Auth'd
        dynamic content must say no-store itself; never rely on the CDN's
        defaults being conservative."""
        super().send_response(code, message)
        self.send_header("Cache-Control", "no-store")

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

    # -- authentication ---------------------------------------------------

    def _cookie_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "rt_session":
                return value
        return ""

    def _identity(self) -> Optional[Tuple[str, bool]]:
        """(username, is_admin) for this request, or None = must log in.

        Same-machine tools (the GUI's Watch-in-VLC on Windows binds
        loopback-only) skip the login and count as an admin -- that's
        also the recovery path if every PIN is forgotten. But only when
        the request didn't come through a Cloudflare Tunnel, whose
        cloudflared daemon runs on this host and delivers *internet*
        traffic from loopback: tunnel requests always carry
        CF-Connecting-IP, and a LAN client faking that header only makes
        itself stricter.
        """
        if (self.client_address[0] in ("127.0.0.1", "::1")
                and "CF-Connecting-IP" not in self.headers):
            return (_LOCAL_USER, True)
        if not _auth_enabled():
            return ("", False)  # open mode: anyone may view, nobody manages
        user = _session_user(self._cookie_token())
        if user is not None:
            return (user, _store.is_admin(user))
        auth_header = self.headers.get("Authorization") or ""
        if auth_header.startswith("Basic "):
            try:
                import base64
                user, _, pin = base64.b64decode(
                    auth_header[6:].strip()).decode("utf-8").partition(":")
            except Exception:
                return None
            matched, _ = _check_credentials(user, pin)
            if matched is not None:
                return (matched, _store.is_admin(matched))
        return None

    def _send_login(self, head_only: bool, error: str = "", status: int = 401) -> None:
        page = _login_page(self.path, error)
        self.send_response(status)
        # The WWW-Authenticate challenge is what makes VLC (given
        # http://user:pin@host/...) retry with credentials -- but it also
        # makes browsers pop their native login box over our form, so
        # only send it to clients that didn't ask for HTML.
        if "text/html" not in (self.headers.get("Accept") or ""):
            self.send_header("WWW-Authenticate", 'Basic realm="reolink-timelapse"')
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        if not head_only:
            self.wfile.write(page)

    def _read_form(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            return parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, OSError):
            return {}

    def _handle_login_post(self) -> None:
        form = self._read_form()
        user = (form.get("username") or [""])[0]
        pin = (form.get("pin") or [""])[0]
        next_path = _safe_next((form.get("next") or ["/"])[0])
        matched, locked_for = _check_credentials(user, pin)
        if matched is None:
            error = (f"Too many wrong attempts -- locked for {int(locked_for) + 1}s."
                     if locked_for else "Wrong username or PIN.")
            self._send_login(head_only=False, error=error,
                            status=429 if locked_for else 401)
            return
        self.send_response(303)
        self.send_header("Location", next_path)
        self.send_header(
            "Set-Cookie",
            f"rt_session={_new_session(matched)}; Path=/; Max-Age={_SESSION_SECONDS}; "
            f"HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_users(self, identity: Tuple[str, bool], head_only: bool,
                      message: str = "", error: str = "") -> None:
        """The admin's user-management page. Mutations arrive as POSTs
        (cross-site POSTs can't ride the SameSite=Lax cookie, which is
        the CSRF defence)."""
        if not identity[1]:
            self.send_error(403, "Only an admin can manage users")
            return
        rows = "".join(
            f"<li><b>{html.escape(u['name'])}</b>"
            + (" <span class='m'>(admin)</span>" if u["admin"] else "")
            + f"<form method='post' action='/users/remove' style='display:inline'>"
              f"<input type='hidden' name='username' value='{html.escape(u['name'], quote=True)}'>"
              f" <button>remove</button></form></li>"
            for u in (_store.list_users() if _store else [])
        ) or "<li class='m'>(no users yet -- everyone can view until one is added)</li>"
        note = (f"<p style='color:#070'>{html.escape(message)}</p>" if message else "") + \
               (f"<p style='color:#b00'>{html.escape(error)}</p>" if error else "")
        page = _page(
            "Users",
            f"<p><a href='/'>&larr; cameras</a></p><h2>Users</h2>{note}"
            f"<ul>{rows}</ul>"
            f"<h3>Add user (or reset a PIN)</h3>"
            f"<form method='post' action='/users/add'>"
            f"<p><label>Username<br><input name='username' style='font-size:1.1em;padding:.3em'></label></p>"
            f"<p><label>PIN (4+ characters; longer or wordier = safer)<br>"
            f"<input name='pin' type='password' style='font-size:1.1em;padding:.3em'></label></p>"
            f"<p><label><input type='checkbox' name='admin' value='1'> can manage users</label></p>"
            f"<p><button style='font-size:1.05em;padding:.35em 1.2em'>Save</button></p></form>"
            f"<p class='m'>Saving an existing username resets that person's PIN. "
            f"Removing a user logs their devices out immediately.</p>",
        )
        self._send_html(page, head_only)

    def _handle_users_post(self, identity: Tuple[str, bool], action: str) -> None:
        if not identity[1]:
            self.send_error(403, "Only an admin can manage users")
            return
        form = self._read_form()
        username = (form.get("username") or [""])[0].strip()
        try:
            if action == "add":
                _store.put(username, (form.get("pin") or [""])[0],
                           admin=bool(form.get("admin")))
                msg = f"Saved '{username}'."
            else:
                _store.remove(username)
                _drop_user_sessions(username)
                msg = f"Removed '{username}'."
        except ValueError as e:
            self._handle_users(identity, head_only=False, error=str(e))
            return
        self._handle_users(identity, head_only=False, message=msg)

    def _handle_logout(self) -> None:
        with _auth_lock:
            _sessions.pop(self._cookie_token(), None)
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie",
                         "rt_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routing ----------------------------------------------------------

    def _handle(self, head_only: bool) -> None:
        try:
            raw = self.path.split("?", 1)[0]
            parts = [unquote(p) for p in raw.strip("/").split("/")] if raw.strip("/") else []
            if parts == ["logout"]:
                self._handle_logout()
                return
            identity = self._identity()
            if identity is None:
                self._send_login(head_only)
                return
            if not parts:
                self._send_html(_root_index(identity), head_only)
            elif parts == ["users"]:
                self._handle_users(identity, head_only)
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

    def do_POST(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path == "/login":
                self._handle_login_post()
                return
            if path in ("/users/add", "/users/remove"):
                identity = self._identity()
                if identity is None:
                    self._send_login(head_only=False)
                else:
                    self._handle_users_post(identity, path.rsplit("/", 1)[1])
                return
            self.send_error(404)
        except (ConnectionError, OSError):
            pass


def start_stream_server(host: Optional[str] = None, log: Callable[[str], None] = print,
                        auth: Optional[Tuple[str, str]] = None) -> None:
    """Start the server on a daemon thread. Idempotent; a busy port is
    logged and tolerated -- the app must keep working without the server.

    `host` overrides the platform default (see default_bind_host()) --
    pass a Config's stream_bind_host, or leave it as None to use the
    default. Accounts live in the stream_users.yaml store (managed from
    the web UI's /users page); `auth` is the legacy single (username,
    pin) config pair, imported once as the first admin account when no
    users file exists yet. With no users anywhere the server stays open,
    the original trusted-LAN posture. If the serving thread ever dies,
    that is logged and the dead server is forgotten, so the next call
    here brings it back -- the Watch in VLC button calls this before
    every launch for exactly that reason.
    """
    global _server, _bind_host, _store
    with _server_lock:
        if _store is None:
            from .webusers import UserStore
            store = UserStore()
            if auth and auth[0] and auth[1]:
                store.seed_if_empty(str(auth[0]), str(auth[1]))
            _store = store
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
            if _auth_enabled():
                log(f"Live stream server listening on {host}:{STREAM_PORT} -- "
                    f"username + PIN login required.")
            else:
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
