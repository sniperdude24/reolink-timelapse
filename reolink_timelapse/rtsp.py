"""ffmpeg/RTSP helpers shared by capture and build."""

from __future__ import annotations

import shutil
import sys
from urllib.parse import quote

from .config import Setup


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit(
            "ERROR: ffmpeg was not found on your PATH.\n"
            "Install it and make sure 'ffmpeg -version' works in a terminal first.\n"
            "Windows builds: https://www.gyan.dev/ffmpeg/builds/"
        )


def build_rtsp_url(setup: Setup) -> str:
    channel = f"{setup.channel:02d}"
    stream = "sub" if setup.substream else "main"
    user = quote(setup.user, safe="")
    password = quote(setup.password, safe="")
    return f"rtsp://{user}:{password}@{setup.ip}:{setup.port}/h264Preview_{channel}_{stream}"
