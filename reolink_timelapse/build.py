"""Stitch captured frames into an mp4, with optional date range filtering.

Before building, frames are scanned for this camera family's signature
decode artifact -- a thin vivid-green vertical line at an HEVC tile
boundary (the nonconforming tiled stream occasionally loses a NALU and the
tile edge mis-stitches, persisting until the next keyframe). Flagged
frames are deleted so they never appear in a video: the line only ever
shows as one isolated saturated-green column, which nothing in a real
scene (grass and trees are broad and far less saturated) reproduces.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from PIL import Image

from .config import Setup
from .rtsp import check_ffmpeg, no_console_kwargs

FRAME_STEM_LEN = len("20260815_143000")  # YYYYMMDD_HHMMSS
GREEN_LINE_MIN_HEIGHT_FRAC = 0.25  # line must span at least this much of the frame


def _frame_date(filename: str) -> Optional[dt.date]:
    stem = filename[:FRAME_STEM_LEN]
    try:
        return dt.datetime.strptime(stem, "%Y%m%d_%H%M%S").date()
    except ValueError:
        return None


def has_green_line(path: Path) -> bool:
    """True when the frame carries the green tile-boundary line artifact
    (or can't be decoded at all, which would break the build anyway)."""
    try:
        im = Image.open(path)
        im.draft("RGB", (384, 216))  # fast JPEG partial decode
        im = im.convert("RGB").resize((192, 108))
    except Exception:
        return True
    w, h = im.size
    px = im.load()
    counts = [0] * w
    for x in range(w):
        c = 0
        for y in range(h):
            r, g, b = px[x, y]
            if g > 120 and g > 1.6 * r and g > 1.6 * b:
                c += 1
        counts[x] = c
    need = h * GREEN_LINE_MIN_HEIGHT_FRAC
    for x in range(w):
        if counts[x] >= need:
            left = counts[x - 3] if x >= 3 else 0
            right = counts[x + 3] if x + 3 < w else 0
            if left < counts[x] / 2 and right < counts[x] / 2:  # isolated column
                return True
    return False


def prune_corrupt_frames(frame_paths: List[Path]) -> List[Path]:
    """Delete frames flagged by has_green_line; return the survivors."""
    kept: List[Path] = []
    removed = 0
    for p in frame_paths:
        if has_green_line(p):
            try:
                os.remove(p)
            except OSError:
                pass
            removed += 1
        else:
            kept.append(p)
    if removed:
        print(f"Removed {removed} corrupted frame(s) (green tile-boundary line).")
    return kept


def _collect_frames(frames_dir: str) -> List[Path]:
    # Frames live one level down, in a per-session subfolder
    # (frames_dir/<session>/*.jpg). Also match frames directly in
    # frames_dir for backward compatibility with captures made before
    # per-session folders existed. Sorting by filename (not full path)
    # keeps chronological order regardless of which session a frame is in,
    # since the timestamp is encoded in the filename itself.
    root = Path(frames_dir)
    frames = list(root.glob("*/*.jpg")) + list(root.glob("*.jpg"))
    return sorted(frames, key=lambda p: p.name)


def build_timelapse(
    setup: Setup,
    output_fps: float = 30,
    output_file: Optional[str] = None,
    start_date: Optional[dt.date] = None,
    end_date: Optional[dt.date] = None,
    frames_dir: Optional[str] = None,
    smooth_base_fps: Optional[float] = None,
) -> str:
    # smooth_base_fps: motion-smoothed build. Captured frames play at this
    # rate and minterpolate synthesizes the in-between frames up to
    # output_fps, so sparse captures come out fluid instead of jerky (and
    # proportionally longer). Must be > 0 and < output_fps. Rendering is
    # much slower than a plain build -- motion estimation on every frame.
    if smooth_base_fps is not None and not 0 < smooth_base_fps < output_fps:
        sys.exit("ERROR: smoothing base fps must be greater than 0 and "
                 "less than the output fps.")
    ffmpeg_bin = check_ffmpeg()
    frames_dir = frames_dir or setup.frames_dir
    if not os.path.isdir(frames_dir):
        sys.exit(f"ERROR: frames directory '{frames_dir}' does not exist.")

    frame_paths = _collect_frames(frames_dir)
    if start_date or end_date:
        frame_paths = [
            p for p in frame_paths
            if (d := _frame_date(p.name)) is not None
            and (start_date is None or d >= start_date)
            and (end_date is None or d <= end_date)
        ]
    frame_paths = prune_corrupt_frames(frame_paths)
    if not frame_paths:
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
        for p in frame_paths:
            abs_path = str(p.resolve())
            escaped = abs_path.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    if smooth_base_fps is not None:
        # Real frames anchor at the base rate; minterpolate (motion-
        # compensated, with its default scene-change detection so big jumps
        # get duplicated rather than morphed) fills up to output_fps.
        # deflicker stays first so it runs on real frames only.
        input_fps = smooth_base_fps
        vf = (f"deflicker,minterpolate=fps={output_fps}:mi_mode=mci:"
              f"mc_mode=aobmc:me_mode=bidir:vsbmc=1")
    else:
        input_fps = output_fps
        vf = "deflicker"

    cmd = [
        ffmpeg_bin, "-y",
        "-f", "concat", "-safe", "0",
        "-r", str(input_fps),
        "-i", list_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        output_file,
    ]

    if smooth_base_fps is not None:
        print(f"Building motion-smoothed timelapse from {len(frame_paths)} frames "
              f"({smooth_base_fps:g} real fps interpolated up to {output_fps:g} fps) -- "
              f"this renders much slower than a plain build...")
    else:
        print(f"Building timelapse at {output_fps} fps from {len(frame_paths)} frames...")
    try:
        subprocess.run(cmd, check=True, **no_console_kwargs())
    except subprocess.CalledProcessError as e:
        sys.exit(f"ffmpeg exited with an error (code {e.returncode}) while building the video.")
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass

    return os.path.abspath(output_file)
