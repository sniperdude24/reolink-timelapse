"""Stitch captured frames into an mp4, with optional date range filtering."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from typing import Optional

from .config import Setup
from .rtsp import check_ffmpeg

FRAME_STEM_LEN = len("20260815_143000")  # YYYYMMDD_HHMMSS


def _frame_date(filename: str) -> Optional[dt.date]:
    stem = filename[:FRAME_STEM_LEN]
    try:
        return dt.datetime.strptime(stem, "%Y%m%d_%H%M%S").date()
    except ValueError:
        return None


def build_timelapse(
    setup: Setup,
    output_fps: float = 30,
    output_file: Optional[str] = None,
    start_date: Optional[dt.date] = None,
    end_date: Optional[dt.date] = None,
) -> str:
    ffmpeg_bin = check_ffmpeg()
    frames_dir = setup.frames_dir
    if not os.path.isdir(frames_dir):
        sys.exit(f"ERROR: frames directory '{frames_dir}' does not exist.")

    frame_files = sorted(f for f in os.listdir(frames_dir) if f.lower().endswith(".jpg"))
    if start_date or end_date:
        frame_files = [
            f for f in frame_files
            if (d := _frame_date(f)) is not None
            and (start_date is None or d >= start_date)
            and (end_date is None or d <= end_date)
        ]
    if not frame_files:
        sys.exit(f"ERROR: no matching .jpg frames found in '{frames_dir}'.")

    os.makedirs(setup.output_dir, exist_ok=True)
    if not output_file:
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(setup.output_dir, f"{setup.name}_{timestamp}.mp4")

    # Concat demuxer + explicit file list instead of "-pattern_type glob":
    # many Windows ffmpeg builds are compiled without POSIX glob() and fail
    # with "Pattern type 'glob' is not available".
    list_path = os.path.join(frames_dir, "_timelapse_concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for name in frame_files:
            abs_path = os.path.abspath(os.path.join(frames_dir, name))
            escaped = abs_path.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    cmd = [
        ffmpeg_bin, "-y",
        "-f", "concat", "-safe", "0",
        "-r", str(output_fps),
        "-i", list_path,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        output_file,
    ]

    print(f"Building timelapse at {output_fps} fps from {len(frame_files)} frames...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(f"ffmpeg exited with an error (code {e.returncode}) while building the video.")
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass

    return os.path.abspath(output_file)
