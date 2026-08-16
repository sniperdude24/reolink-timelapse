"""Persistent, multi-setup configuration.

Config lives outside the repo/working directory (an OS-appropriate user
config dir) so the same install can drive any number of camera setups
without editing files by hand, and so credentials never end up committed
to git by accident.
"""

from __future__ import annotations

import os
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


def default_config_path() -> Path:
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
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self.setups = {
            name: Setup.from_dict(name, data)
            for name, data in (raw.get("setups") or {}).items()
        }

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
