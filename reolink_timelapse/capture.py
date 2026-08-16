"""Start/stop the ffmpeg frame-capture process for one setup."""

from __future__ import annotations

import os
import subprocess

from .config import Setup
from .rtsp import build_rtsp_url, check_ffmpeg


def start_capture_process(setup: Setup) -> subprocess.Popen:
    ffmpeg_bin = check_ffmpeg()
    os.makedirs(setup.frames_dir, exist_ok=True)
    rtsp_url = build_rtsp_url(setup)
    fps_filter = f"fps=1/{setup.interval}"
    out_pattern = os.path.join(setup.frames_dir, "%Y%m%d_%H%M%S.jpg")

    cmd = [
        ffmpeg_bin,
        "-rtsp_transport", "tcp",
        "-timeout", "10000000",  # 10s connect/read timeout (microseconds)
        "-i", rtsp_url,
        "-vf", fps_filter,
        "-q:v", "2",
        "-strftime", "1",
        out_pattern,
    ]
    return subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )


def stop_capture_process(proc: subprocess.Popen, timeout: float = 10) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
