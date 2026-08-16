"""Persistent, multi-setup configuration.

Config, frames, and videos all default to living inside one "program
folder" -- the running .exe's own folder when packaged, or the repo root
when running from source -- so a whole install (app + settings + captured
media) can be copied to another machine or a USB drive as a single unit.
Per-setup frames_dir/output_dir can still be pointed elsewhere.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def app_root_dir() -> Path:
    """The single "program folder" config.yaml and Timelapses/ default into.

    Frozen (.exe): the exe's own folder -- mirrors rtsp.resolve_ffmpeg()'s
    frozen check. Source run: the repo/install root, two levels up from
    this file.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def default_setup_dirs(name: str) -> tuple[Path, Path]:
    """Default (frames_dir, output_dir) for a new setup, under the program
    folder's Timelapses/<name>/. The one place this is computed -- used by
    both the CLI wizard and the GUI's Add/Edit dialog."""
    base = app_root_dir() / "Timelapses" / name
    return base / "frames", base


def default_config_path() -> Path:
    return app_root_dir() / "config.yaml"


def _legacy_config_path() -> Path:
    """Where config.yaml lived before config/data moved into the program
    folder (the OS user config dir). Kept only so Config.load() can
    migrate an existing install's config forward automatically."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "reolink-timelapse" / "config.yaml"


@dataclass
class Schedule:
    mode: str = "daylight"  # "daylight" or "always"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[str] = None
    pre_offset_minutes: int = 0
    post_offset_minutes: int = 0


@dataclass
class Setup:
    name: str
    ip: str
    user: str
    password: str
    port: int = 554
    channel: int = 1
    substream: bool = True
    interval: float = 30
    frames_dir: str = "frames"
    output_dir: str = "."
    schedule: Schedule = field(default_factory=Schedule)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "Setup":
        sched = Schedule(**data.get("schedule", {}))
        kwargs = {k: v for k, v in data.items() if k != "schedule"}
        return cls(name=name, schedule=sched, **kwargs)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("name")
        return d


class Config:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or default_config_path()
        self.setups: dict[str, Setup] = {}
        self.migrated_from: Optional[Path] = None
        self.migration_error: Optional[str] = None
        self.load()

    def load(self) -> None:
        self._migrate_legacy_config()
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self.setups = {
            name: Setup.from_dict(name, data)
            for name, data in (raw.get("setups") or {}).items()
        }

    def _migrate_legacy_config(self) -> None:
        # Never migrate under a caller-supplied path (e.g. tests) -- only
        # the real default location gets the one-time upgrade treatment.
        if self.path.exists() or self.path != default_config_path():
            return
        legacy = _legacy_config_path()
        if not legacy.exists() or legacy == self.path:
            return
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                yaml.safe_load(f)  # validate before trusting it
        except Exception as e:
            self.migration_error = (
                f"Found an old config at {legacy} but couldn't read it ({e}); left it in place."
            )
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, self.path)
            self.migrated_from = legacy
        except OSError as e:
            self.migration_error = (
                f"Found an old config at {legacy} but couldn't copy it to {self.path} ({e})."
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {"setups": {name: s.to_dict() for name, s in self.setups.items()}}
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, sort_keys=False)
        if sys.platform != "win32":
            os.chmod(self.path, 0o600)  # config holds camera passwords

    def get(self, name: str) -> Setup:
        try:
            return self.setups[name]
        except KeyError:
            available = ", ".join(sorted(self.setups)) or "(none configured yet)"
            raise SystemExit(
                f"No setup named '{name}'. Configured setups: {available}"
            )

    def put(self, setup: Setup) -> None:
        self.setups[setup.name] = setup

    def remove(self, name: str) -> None:
        del self.setups[name]
