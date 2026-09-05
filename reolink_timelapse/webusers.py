"""User accounts for the stream server's login, managed from the web UI.

Users live in stream_users.yaml next to config.yaml -- their own file so
the web server can add/remove accounts on the fly without racing the
GUI/CLI over config.yaml. PINs (any secret 4+ characters; digits or a
passphrase) are stored as salted PBKDF2-SHA256 hashes, never plaintext:
config.yaml already holds camera passwords in the clear out of
necessity (ffmpeg needs them), but login secrets have no such excuse.
The file is chmod 0600 and written atomically (tmp + os.replace), same
conventions as config.py.

Back-compat seed: the older single-account config keys
(stream.auth_user/auth_pin) are imported as the first admin account the
first time the server starts with no users file, so an existing setup
upgrades without anyone re-creating their login.

Every mutation takes a lock and rewrites the file, then reloads on next
read-through -- at household scale (a handful of users, mutations a few
times a year) simplicity beats cleverness.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sys
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .config import app_root_dir

_PBKDF2_ITERATIONS = 100_000  # ~50ms on a Pi 5: negligible per login,
                              # expensive at brute-force scale
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
MIN_PIN_LEN = 4


def users_file_path() -> Path:
    return app_root_dir() / "stream_users.yaml"


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2:{_PBKDF2_ITERATIONS}:{salt}:{digest}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split(":")
        if scheme != "pbkdf2":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", pin.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, AttributeError):
        return False


def valid_username(name: str) -> bool:
    return bool(_USERNAME_RE.fullmatch(name))


class UserStore:
    """The stream server's account list. Usernames are matched
    case-insensitively (phone keyboards autocapitalize) but displayed as
    entered."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or users_file_path()
        self._lock = threading.Lock()
        self._users: Dict[str, dict] = {}  # casefolded name -> record
        self._mtime: Optional[int] = None  # file state the cache reflects
        self._load()

    def _file_mtime(self) -> Optional[int]:
        try:
            return os.stat(self._path).st_mtime_ns
        except OSError:
            return None

    def _refresh(self) -> None:
        """Reload when the file changed underneath us -- the `users` CLI
        (e.g. `docker exec ... users add`) edits the same file while the
        server is running, and its changes must take effect without a
        restart. Called with the lock held."""
        if self._file_mtime() != self._mtime:
            self._load()

    def _load(self) -> None:
        self._mtime = self._file_mtime()
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self._users = {}
            return
        except (OSError, yaml.YAMLError):
            # An unreadable users file must fail CLOSED (nobody gets in),
            # not open -- the loopback exemption keeps the owner able to
            # recover from the host machine itself.
            self._users = {"\x00unreadable": {"name": "\x00unreadable",
                                              "pin_hash": "!", "admin": False}}
            return
        users = {}
        for name, rec in (raw.get("users") or {}).items():
            if isinstance(rec, dict) and rec.get("pin_hash"):
                users[str(name).casefold()] = {
                    "name": str(name),
                    "pin_hash": str(rec["pin_hash"]),
                    "admin": bool(rec.get("admin", False)),
                }
        self._users = users

    def _write(self) -> None:
        raw = {"users": {
            rec["name"]: {"pin_hash": rec["pin_hash"], "admin": rec["admin"]}
            for rec in self._users.values() if not rec["name"].startswith("\x00")
        }}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(raw, f, sort_keys=False)
            if sys.platform != "win32":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
            self._mtime = self._file_mtime()
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- queries ----------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._users)

    def list_users(self) -> List[dict]:
        with self._lock:
            self._refresh()
            return sorted(
                ({"name": r["name"], "admin": r["admin"]}
                 for r in self._users.values() if not r["name"].startswith("\x00")),
                key=lambda r: r["name"].casefold())

    def verify(self, username: str, pin: str) -> Optional[dict]:
        """The user's record when the credentials are right, else None.
        Always burns a hash computation so a wrong username costs the
        same time as a wrong PIN."""
        with self._lock:
            self._refresh()
            rec = self._users.get(username.casefold())
        if rec is None:
            verify_pin(pin, hash_pin("decoy"))  # constant-time-ish miss
            return None
        return dict(rec) if verify_pin(pin, rec["pin_hash"]) else None

    def is_admin(self, username: str) -> bool:
        with self._lock:
            self._refresh()
            rec = self._users.get(username.casefold())
        return bool(rec and rec["admin"])

    # -- mutations --------------------------------------------------------

    def put(self, username: str, pin: str, admin: bool = False) -> None:
        """Add a user, or reset an existing user's PIN (same action --
        that's how a forgotten PIN gets fixed). Raises ValueError with a
        user-showable message on bad input."""
        username = username.strip()
        if not valid_username(username):
            raise ValueError("Usernames are 1-32 letters, digits, . _ or -")
        if len(pin) < MIN_PIN_LEN:
            raise ValueError(f"PIN must be at least {MIN_PIN_LEN} characters.")
        with self._lock:
            self._refresh()
            existing = self._users.get(username.casefold())
            if existing:
                admin = admin or existing["admin"]  # updating never demotes
            self._users[username.casefold()] = {
                "name": username, "pin_hash": hash_pin(pin), "admin": admin}
            self._write()

    def remove(self, username: str) -> None:
        """Raises ValueError when removal would leave no admin able to
        manage users (the lock-yourself-out guard)."""
        with self._lock:
            self._refresh()
            key = username.casefold()
            rec = self._users.get(key)
            if rec is None:
                return
            if rec["admin"] and not any(
                    r["admin"] for k, r in self._users.items() if k != key):
                raise ValueError("Can't remove the last admin.")
            del self._users[key]
            self._write()

    def seed_if_empty(self, username: Optional[str], pin: Optional[str]) -> None:
        """Import the old single-account config keys as the first admin,
        once, so existing setups keep their login across the upgrade."""
        if self._path.exists() or not username or not pin:
            return
        try:
            self.put(str(username), str(pin), admin=True)
        except ValueError:
            pass  # an unusable legacy value just means no seed
