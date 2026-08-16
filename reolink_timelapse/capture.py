"""Start/stop the ffmpeg frame-capture process for one setup.

Built for day+ unattended runs:

- stderr is kept quiet (-loglevel error -nostats) AND continuously drained
  into a small bounded buffer by a background thread. Without the drain, any
  sustained stderr output (progress stats, decoder warnings) eventually
  fills the ~64KB pipe buffer and ffmpeg blocks mid-write -- capture
  silently freezes with the process still "running". The drained tail is
  what the scheduler shows when ffmpeg dies unexpectedly.
- Frames are thinned with a select filter rather than fps=1/interval: select
  passes real source frames spaced at least `interval` apart and never
  fabricates duplicates when the interval is shorter than the stream's
  frame spacing (fps= pads to the target rate, which at sub-second
  intervals wrote mostly-duplicate 4K JPEGs).
- Filenames are <run start time>_<sequence>.jpg. strftime-only names had
  1-second resolution, so sub-second frames overwrote each other; the
  sequence suffix makes every frame unique at any interval while the
  15-char timestamp prefix keeps build.py's date filtering and
  chronological name-sorting working.
- Decode is deliberately software-only. GPU decode (-hwaccel auto /
  NVDEC) was tried for the CPU saving, but on this camera family's
  nonconforming tiled HEVC (PPS re-sent mid-frame) NVDEC mis-stitches
  tile boundaries into a vivid-green vertical line: decoding the same
  recorded 10-minute stream twice gave 0 line frames in software vs 401
  with NVDEC. Correct frames beat the ~25%-of-one-core decode cost.
- Two layers against green-smear corruption (verified against a
  deliberately damaged copy of the real stream): -fflags discardcorrupt
  drops the packets/frames ffmpeg actually flags as corrupt, and -- the
  layer that does the heavy lifting -- intervals of
  KEYFRAME_SNAP_MIN_INTERVAL seconds or more save only keyframes
  (I-frames). HEVC error concealment hands back smeared P-frames without
  flagging them, and one hit smears every frame until the next keyframe;
  keyframes reference nothing, so snapping to them cut visibly damaged
  saves from 75/176 to 0/9 on the same damaged input. Spacing just rounds
  up to the camera's keyframe cadence (~2s on Reolink). Shorter intervals
  keep saving every frame -- sub-second capture still works. (-skip_frame
  nokey would also skip the decode work, but this camera re-sends its PPS
  mid-frame, which that path can't decode at all.)
- Stop is graceful-first: ffmpeg's 'q' command via stdin lets it finish
  writing the current frame, so the last file of a session is never a
  truncated JPEG that then breaks the session's auto-build.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import threading
from collections import deque

from .config import Setup
from .rtsp import build_rtsp_url, check_ffmpeg, no_console_kwargs

STDERR_TAIL_LINES = 50
GRACEFUL_STOP_TIMEOUT = 5
KEYFRAME_SNAP_MIN_INTERVAL = 5  # seconds; at/above this, save keyframes only


def _drain_stderr(proc: subprocess.Popen) -> None:
    try:
        for line in proc.stderr:
            proc.stderr_tail.append(line)
    except (ValueError, OSError):
        pass  # pipe closed during shutdown


def read_capture_stderr(proc: subprocess.Popen) -> str:
    """The last few lines ffmpeg wrote to stderr (drained continuously)."""
    tail = getattr(proc, "stderr_tail", None)
    return "".join(tail) if tail else ""


def start_capture_process(setup: Setup, session_dir: str) -> subprocess.Popen:
    ffmpeg_bin = check_ffmpeg()
    os.makedirs(session_dir, exist_ok=True)
    rtsp_url = build_rtsp_url(setup)
    # \, -- comma is a filter-graph separator and must be escaped inside the
    # select expression.
    spacing = f"isnan(prev_selected_t)+gte(t-prev_selected_t\\,{setup.interval})"
    if setup.interval >= KEYFRAME_SNAP_MIN_INTERVAL:
        thin_filter = f"select=eq(pict_type\\,I)*({spacing})"
    else:
        thin_filter = f"select={spacing}"
    run_start = dt.datetime.now()
    out_pattern = os.path.join(session_dir, f"{run_start:%Y%m%d_%H%M%S}_%06d.jpg")

    cmd = [
        ffmpeg_bin,
        "-loglevel", "error", "-nostats",
        "-fflags", "discardcorrupt",
        "-rtsp_transport", "tcp",
        "-timeout", "10000000",  # 10s connect/read timeout (microseconds)
        "-i", rtsp_url,
        "-an",
        "-vf", thin_filter,
        "-fps_mode", "vfr",
        "-q:v", "2",
        out_pattern,
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, **no_console_kwargs()
    )
    proc.stderr_tail = deque(maxlen=STDERR_TAIL_LINES)
    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()
    return proc


def stop_capture_process(proc: subprocess.Popen, timeout: float = 10) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.stdin.write("q\n")
        proc.stdin.flush()
        proc.wait(timeout=GRACEFUL_STOP_TIMEOUT)
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass  # stdin already closed, or ffmpeg ignored 'q' -- force it
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
