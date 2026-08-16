"""ffmpeg/RTSP helpers shared by capture and build."""

from __future__ import annotations

import shutil
import sys
from typing import Optional
from urllib.parse import quote

from .config import Setup, app_root_dir


def resolve_ffmpeg() -> Optional[str]:
    """Path/command to invoke ffmpeg with.

    When packaged with PyInstaller, ffmpeg.exe is bundled in the program
    folder (see config.app_root_dir) -- prefer that over whatever's on
    PATH so the packaged tool works with no external ffmpeg install. In a
    normal Python run, fall back to PATH lookup instead, since ffmpeg
    isn't bundled in a source checkout.
    """
    if getattr(sys, "frozen", False):
        bundled = app_root_dir() / "ffmpeg.exe"
        if bundled.exists():
            return str(bundled)
    return shutil.which("ffmpeg")


def check_ffmpeg() -> str:
    path = resolve_ffmpeg()
    if path is None:
        sys.exit(
            "ERROR: ffmpeg was not found on your PATH.\n"
            "Install it and make sure 'ffmpeg -version' works in a terminal first.\n"
            "Windows builds: https://www.gyan.dev/ffmpeg/builds/"
        )
    return path


def build_rtsp_url(setup: Setup) -> str:
    channel = f"{setup.channel:02d}"
    stream = "sub" if setup.substream else "main"
    user = quote(setup.user, safe="")
    password = quote(setup.password, safe="")
    return f"rtsp://{user}:{password}@{setup.ip}:{setup.port}/h264Preview_{channel}_{stream}"
