"""Persistent, multi-setup configuration.

Config, frames, and videos all live inside one "program folder" -- the
running .exe's own folder when packaged, or the repo root when running
from source -- so a whole install (app + settings + captured media) can be
copied to another machine or a USB drive as a single unit. Storage
locations are derived from each recording's name, not configured.
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
    """The (frames_dir, output_dir) for a recording, always under the
    program folder's Timelapses/<name>/. The one place this is computed --
    everything else derives storage from here."""
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
    mode: str = "daylight"  # "daylight" | "always" | "fixed_time" | "duration"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[str] = None
    pre_offset_minutes: int = 0
    post_offset_minutes: int = 0
    start_time: Optional[str] = None  # "HH:MM", fixed_time mode
    end_time: Optional[str] = None  # "HH:MM", fixed_time mode
    duration_minutes: Optional[int] = None  # duration mode


@dataclass
class Setup:
    """Runtime view of everything needed to capture/build for one recording --
    a Camera's source fields merged with a Recording's schedule/output fields.
    Not stored directly; see Recording.to_setup()."""
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
    output_fps: float = 30
    target_video_seconds: Optional[int] = None
    schedule: Schedule = field(default_factory=Schedule)


@dataclass
class Camera:
    """A reusable RTSP source. Multiple Recordings can reference the same camera."""
    name: str
    ip: str
    user: str
    password: str
    port: int = 554
    channel: int = 1
    substream: bool = True

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "Camera":
        return cls(name=name, **data)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("name")
        return d


@dataclass
class Recording:
    """A schedule configuration for capturing from a named Camera.

    Frames and finished videos always live under the program folder's
    Timelapses/<name>/ -- derived from the name, not stored, so every
    install is self-contained and configs can't point at stale locations.

    Capture pacing: interval-paced by default (one frame every `interval`
    seconds). When `target_video_seconds` is set, the recording is
    length-paced instead -- the scheduler derives the interval fresh each
    session from that session's window duration so the finished video
    comes out the target length at `output_fps`.
    """
    name: str
    camera_name: str
    interval: float = 30
    output_fps: float = 30
    target_video_seconds: Optional[int] = None
    schedule: Schedule = field(default_factory=Schedule)

    @property
    def frames_dir(self) -> str:
        return str(default_setup_dirs(self.name)[0])

    @property
    def output_dir(self) -> str:
        return str(default_setup_dirs(self.name)[1])

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "Recording":
        sched = Schedule(**data.get("schedule", {}))
        # frames_dir/output_dir were stored (and customizable) in older
        # configs -- discard them; storage is derived from the name now.
        kwargs = {k: v for k, v in data.items()
                  if k not in ("schedule", "frames_dir", "output_dir")}
        return cls(name=name, schedule=sched, **kwargs)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("name")
        return d

    def to_setup(self, camera: Camera) -> Setup:
        return Setup(
            name=self.name, ip=camera.ip, user=camera.user, password=camera.password,
            port=camera.port, channel=camera.channel, substream=camera.substream,
            interval=self.interval, frames_dir=self.frames_dir, output_dir=self.output_dir,
            output_fps=self.output_fps, target_video_seconds=self.target_video_seconds,
            schedule=self.schedule,
        )


class Config:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or default_config_path()
        self.cameras: dict[str, Camera] = {}
        self.recordings: dict[str, Recording] = {}
        self.migrated_from: Optional[Path] = None
        self.migration_error: Optional[str] = None
        self.schema_migrated = False
        self.load()

    def load(self) -> None:
        self._migrate_legacy_config()
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if "setups" in raw and "cameras" not in raw and "recordings" not in raw:
            raw = self._convert_setups_schema(raw)
            self.schema_migrated = True
        self.cameras = {
            name: Camera.from_dict(name, data)
            for name, data in (raw.get("cameras") or {}).items()
        }
        self.recordings = {
            name: Recording.from_dict(name, data)
            for name, data in (raw.get("recordings") or {}).items()
        }
        if self.schema_migrated:
            self.save()

    @staticmethod
    def _convert_setups_schema(raw: dict) -> dict:
        """One-time upgrade from the old single-Setup schema (camera source
        and schedule/output welded together) to separate cameras/recordings,
        each keeping the old setup's name so nothing else has to change."""
        camera_fields = ("ip", "user", "password", "port", "channel", "substream")
        cameras = {}
        recordings = {}
        for name, data in (raw.get("setups") or {}).items():
            cameras[name] = {k: data[k] for k in camera_fields if k in data}
            recordings[name] = {
                "camera_name": name,
                "interval": data.get("interval", 30),
                "schedule": data.get("schedule", {}),
            }
        return {"cameras": cameras, "recordings": recordings}

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
        raw = {
            "cameras": {name: c.to_dict() for name, c in self.cameras.items()},
            "recordings": {name: r.to_dict() for name, r in self.recordings.items()},
        }
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, sort_keys=False)
        if sys.platform != "win32":
            os.chmod(self.path, 0o600)  # config holds camera passwords

    def get_camera(self, name: str) -> Camera:
        try:
            return self.cameras[name]
        except KeyError:
            available = ", ".join(sorted(self.cameras)) or "(none configured yet)"
            raise SystemExit(f"No camera named '{name}'. Configured cameras: {available}")

    def get_recording(self, name: str) -> Recording:
        try:
            return self.recordings[name]
        except KeyError:
            available = ", ".join(sorted(self.recordings)) or "(none configured yet)"
            raise SystemExit(f"No recording named '{name}'. Configured recordings: {available}")

    def put_camera(self, camera: Camera) -> None:
        self.cameras[camera.name] = camera

    def put_recording(self, recording: Recording) -> None:
        self.recordings[recording.name] = recording

    def remove_camera(self, name: str) -> None:
        in_use = sorted(r.name for r in self.recordings.values() if r.camera_name == name)
        if in_use:
            raise SystemExit(
                f"Camera '{name}' is still used by recording(s): {', '.join(in_use)}. "
                f"Remove those recordings first."
            )
        del self.cameras[name]

    def remove_recording(self, name: str) -> None:
        del self.recordings[name]

    def resolved(self, name: str) -> Setup:
        """The runtime Setup for a recording: its own schedule/output fields
        merged with its camera's source fields."""
        recording = self.get_recording(name)
        camera = self.get_camera(recording.camera_name)
        return recording.to_setup(camera)
