"""Live rolling timelapse: near-livestream view built from video chunks.

One continuous ffmpeg stream-copies the camera's RTSP feed into gapless
5-minute .ts chunk files (no decode, no encode -- near-zero CPU while
capturing). As each chunk completes, it is converted into a small sped-up
1080p60 segment (60x: 5 minutes -> ~5 seconds, one video frame per real
second), decoded in software -- NVDEC mis-stitches this camera family's
tiled HEVC into green lines, see capture.py. Two always-current outputs
are then refreshed by concat-remuxing segments (-c copy, cheap):

- last_hour.mp4: the trailing window (newest 12 chunks), oldest falls off
- session.mp4: everything since this live session started

Both are written to a temp file and os.replace()d, so opening one never
catches a half-written video. The view lags real time by at most one
chunk.

Disk stays flat by design (user's choice, revised from an earlier
keep-everything policy): each raw chunk is deleted as soon as its segment
is built and both outputs are refreshed (kept only when conversion fails,
for diagnosis), segments are kept for the whole session so its outputs
could be rebuilt, and a new session clears the previous session's
segments (already baked into that session's output files). Chunks left
behind by pre-retention versions are never touched -- they're excluded
from the new session and reported once as safe to delete.

Chunks use the mpegts container so a crash mid-write still leaves a
playable file, and are named by wall-clock start time so they sort
chronologically across capture restarts.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable, List, Optional

from .capture import (
    STDERR_TAIL_LINES,
    _drain_stderr,
    read_capture_stderr,
    stop_capture_process,
)
from .config import Camera, app_root_dir
from .rtsp import build_rtsp_url, check_ffmpeg, no_console_kwargs

LIVE_CHUNK_SECONDS = 300
LIVE_SPEEDUP = 60           # 5 min of real time -> ~5s of video
LIVE_OUTPUT_FPS = 60        # with 60x speed: one video frame per real second
LIVE_OUTPUT_WIDTH = 1920    # segments/outputs downscale to 1080p
LIVE_WINDOW_CHUNKS = 12     # last_hour.mp4 covers this many chunks
CAPTURE_RETRY_SECONDS = 5


def live_dirs(camera_name: str) -> tuple[Path, Path, Path]:
    """(chunks_dir, segments_dir, root) for a camera's live view, under
    the program folder like all other storage."""
    root = app_root_dir() / "Timelapses" / "Live" / camera_name
    return root / "chunks", root / "segments", root


def start_live_capture(camera: Camera, chunks_dir: Path,
                       chunk_seconds: int = LIVE_CHUNK_SECONDS) -> subprocess.Popen:
    ffmpeg_bin = check_ffmpeg()
    os.makedirs(chunks_dir, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-loglevel", "error", "-nostats",
        "-rtsp_transport", "tcp",
        "-timeout", "10000000",
        "-i", build_rtsp_url(camera),
        "-map", "0:v", "-an", "-c", "copy",
        "-f", "segment", "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1", "-strftime", "1",
        os.path.join(str(chunks_dir), "%Y%m%d_%H%M%S.ts"),
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, **no_console_kwargs()
    )
    proc.stderr_tail = deque(maxlen=STDERR_TAIL_LINES)
    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()
    return proc


def convert_chunk(chunk: Path, segments_dir: Path,
                  speedup: float = LIVE_SPEEDUP,
                  fps: float = LIVE_OUTPUT_FPS) -> Path:
    """One raw chunk -> one small sped-up mp4 segment (software decode)."""
    ffmpeg_bin = check_ffmpeg()
    os.makedirs(segments_dir, exist_ok=True)
    out = Path(segments_dir) / f"{chunk.stem}_tl.mp4"
    tmp = Path(segments_dir) / f"{chunk.stem}_tl.tmp.mp4"
    interval = speedup / fps  # real seconds per output frame
    vf = (f"select=isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval}),"
          f"scale={LIVE_OUTPUT_WIDTH}:-2,setpts=N/({fps}*TB)")
    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error", "-nostats",
        "-fflags", "discardcorrupt",
        "-i", str(chunk),
        "-an", "-vf", vf, "-r", str(fps),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, **no_console_kwargs())
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError((r.stderr or "").strip()[-300:] or f"ffmpeg exited {r.returncode}")
    os.replace(tmp, out)
    return out


def refresh_output(segments: List[Path], out_path: Path) -> None:
    """Concat-remux segments into out_path, atomically (temp + replace)."""
    if not segments:
        return
    ffmpeg_bin = check_ffmpeg()
    out_path = Path(out_path)
    list_path = out_path.with_suffix(".list.txt")
    tmp = out_path.with_suffix(".new.mp4")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in segments:
            escaped = str(Path(p).resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    cmd = [ffmpeg_bin, "-y", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(list_path),
           "-c", "copy", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True, **no_console_kwargs())
    try:
        os.remove(list_path)
    except OSError:
        pass
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError((r.stderr or "").strip()[-300:] or f"ffmpeg exited {r.returncode}")
    os.replace(tmp, out_path)


def run_live(camera: Camera, stop_event: threading.Event,
             log: Callable[[str], None] = print,
             status: Optional[Callable[[int, float], None]] = None,
             chunk_seconds: int = LIVE_CHUNK_SECONDS,
             speedup: float = LIVE_SPEEDUP,
             window_chunks: int = LIVE_WINDOW_CHUNKS) -> None:
    """Capture + convert loop; runs until stop_event is set. `status` (if
    given) receives (session_chunk_count, segment_mb) after each chunk."""
    chunks_dir, segments_dir, root = live_dirs(camera.name)
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(segments_dir, exist_ok=True)
    last_hour = root / "last_hour.mp4"
    session_file = root / "session.mp4"

    # Session = since Start. Chunks already in the folder (kept by older
    # versions, or left by a failed conversion) must not be folded into
    # this session -- they'd sort first and prepend old footage. Exclude
    # them, and report them once; they're the user's to delete.
    leftover_chunks = sorted(chunks_dir.glob("*.ts"))
    processed: set = {c.name for c in leftover_chunks}
    if leftover_chunks:
        leftover_gb = sum(c.stat().st_size for c in leftover_chunks) / 1e9
        log(f"Live: {len(leftover_chunks)} raw chunk(s) from earlier runs in "
            f"{chunks_dir} ({leftover_gb:.1f} GB) -- not part of this session, "
            f"safe to delete.")
    # Previous sessions' segments are already baked into that session's
    # output files; clear them so segments/ only ever holds this session.
    for stale in segments_dir.glob("*_tl.mp4"):
        try:
            os.remove(stale)
        except OSError:
            pass

    proc = start_live_capture(camera, chunks_dir, chunk_seconds)
    log(f"Live timelapse started for '{camera.name}': {chunk_seconds // 60}-minute "
        f"chunks at {speedup:g}x speed, 1080p {LIVE_OUTPUT_FPS}fps output. "
        f"Outputs: {last_hour} and {session_file}. "
        f"Raw chunks are deleted once converted.")

    session_segments: List[Path] = []

    def process_chunks(include_newest: bool) -> None:
        chunks = sorted(chunks_dir.glob("*.ts"))
        if not include_newest and proc.poll() is None:
            # the newest file is still being written to
            chunks = chunks[:-1]
        for chunk in chunks:
            if chunk.name in processed:
                continue
            processed.add(chunk.name)
            try:
                seg = convert_chunk(chunk, segments_dir, speedup=speedup)
            except Exception as e:
                log(f"Live: converting {chunk.name} failed: {e}")
                continue
            session_segments.append(seg)
            try:
                refresh_output(session_segments[-window_chunks:], last_hour)
                refresh_output(session_segments, session_file)
            except Exception as e:
                log(f"Live: updating outputs failed: {e}")
                continue
            try:
                os.remove(chunk)  # converted and stitched -- raw copy done
            except OSError as e:
                log(f"Live: couldn't delete converted chunk {chunk.name}: {e}")
            seg_mb = sum(s.stat().st_size for s in session_segments if s.exists()) / 1e6
            log(f"Live: {chunk.name} done -> window "
                f"{min(len(session_segments), window_chunks)}/{window_chunks} chunks, "
                f"session {len(session_segments)} chunks, {seg_mb:.0f} MB of segments")
            if status:
                status(len(session_segments), seg_mb)

    while not stop_event.is_set():
        if proc.poll() is not None:
            log(f"Live: capture for '{camera.name}' exited unexpectedly; retrying in "
                f"{CAPTURE_RETRY_SECONDS}s. ffmpeg said:\n{read_capture_stderr(proc)}")
            process_chunks(include_newest=True)
            if stop_event.wait(CAPTURE_RETRY_SECONDS):
                break
            proc = start_live_capture(camera, chunks_dir, chunk_seconds)
        process_chunks(include_newest=False)
        stop_event.wait(2)

    stop_capture_process(proc)
    process_chunks(include_newest=True)  # convert the final partial chunk too
    log(f"Live timelapse for '{camera.name}' stopped.")
