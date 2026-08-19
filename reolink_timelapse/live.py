"""Live rolling timelapse: near-livestream view built from video chunks.

A thin policy layer over the shared engine in chunks.py. The engine does
the capturing and rendering; what makes this "live" is the pair of
always-current outputs it maintains:

- last_hour.mp4: the trailing window (newest 12 chunks), oldest falls off
- session.mp4: the 6-hour block in progress

Both are written to a temp file and os.replace()d, so opening one never
catches a half-written video. The view lags real time by at most one
chunk. On stop, both are renamed into sessions/ under dated names --
nothing watchable is ever overwritten by the next start.

Disk stays flat by design (user's choice, revised from an earlier
keep-everything policy): each raw chunk is deleted as soon as its segment
is built (kept only when conversion fails, for diagnosis), segments are
kept for the whole session so its outputs could be rebuilt, and a new
session clears the previous session's segments. Chunks left behind by
pre-retention versions are never touched -- they're excluded from the new
session and reported once as safe to delete.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from collections import deque
from pathlib import Path
from typing import Callable, List, Optional

from .capture import read_capture_stderr, stop_capture_process
from .chunks import CHUNK_SECONDS, ChunkRenderer, refresh_output, start_chunk_capture
from .config import Camera, app_root_dir
from .decode import resolve_decoder_for_source
from .rtsp import check_ffmpeg

LIVE_CHUNK_SECONDS = CHUNK_SECONDS
LIVE_SPEEDUP = 60           # 5 min of real time -> ~5s of video
LIVE_OUTPUT_FPS = 60        # with 60x speed: one video frame per real second
LIVE_OUTPUT_WIDTH = 1920    # width; height follows the camera's aspect ratio
LIVE_WINDOW_CHUNKS = 12     # last_hour.mp4 covers this many chunks
SESSION_REFRESH_EVERY = 3   # rebuild session.mp4 every Nth chunk (~15 min)
SESSION_BLOCK_CHUNKS = 72   # close the session and start a new one (~6 hours)
CAPTURE_RETRY_SECONDS = 5


def live_dirs(camera_name: str) -> tuple[Path, Path, Path]:
    """(chunks_dir, segments_dir, root) for a camera's live view, under
    the program folder like all other storage."""
    root = app_root_dir() / "Timelapses" / "Live" / camera_name
    return root / "chunks", root / "segments", root


def _segment_time(segment: Path) -> Optional[dt.datetime]:
    """Wall-clock start of a segment, from its chunk-derived filename."""
    try:
        return dt.datetime.strptime(segment.name[:15], "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _block_filename(camera_name: str, segments: List[Path], suffix: str = "") -> str:
    """`<camera>_<date>_<HHhMM>-<HHhMM><suffix>.mp4` for a finished block,
    using the same %Hh%M label convention as scheduled recordings."""
    start = _segment_time(segments[0])
    end = _segment_time(segments[-1])
    if start is None:
        return f"{camera_name}_{dt.datetime.now():%Y-%m-%d_%Hh%M}{suffix}.mp4"
    end = end or start
    return (f"{camera_name}_{start:%Y-%m-%d}_"
            f"{start:%Hh%M}-{end:%Hh%M}{suffix}.mp4")


def run_live(camera: Camera, stop_event: threading.Event,
             log: Callable[[str], None] = print,
             status: Optional[Callable[[int, float], None]] = None,
             chunk_seconds: int = LIVE_CHUNK_SECONDS,
             speedup: float = LIVE_SPEEDUP,
             window_chunks: int = LIVE_WINDOW_CHUNKS,
             block_chunks: int = SESSION_BLOCK_CHUNKS) -> None:
    """Capture + render loop; runs until stop_event is set.

    Maintains `last_hour.mp4` (a rolling window) and `session.mp4` (the
    block in progress). Every `block_chunks` chunks the session is closed
    off into `sessions/` under a dated name and a new one begins, so a
    long-running camera produces a series of watchable videos instead of
    one that grows forever. `status` (if given) receives
    (block_chunk_count, segment_mb) after each chunk.
    """
    chunks_dir, segments_dir, root = live_dirs(camera.name)
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(segments_dir, exist_ok=True)
    sessions_dir = root / "sessions"
    last_hour = root / "last_hour.mp4"
    session_file = root / "session.mp4"

    hw_decoder = resolve_decoder_for_source(
        camera, camera.decode_mode, check_ffmpeg(), log=lambda m: log(f"Live: {m}"))
    renderer = ChunkRenderer(
        chunks_dir, segments_dir,
        interval=speedup / LIVE_OUTPUT_FPS,  # 60x at 60fps = 1 frame per real second
        output_fps=LIVE_OUTPUT_FPS,
        scale_width=LIVE_OUTPUT_WIDTH,
        hw_decoder=hw_decoder,
        log=lambda m: log(f"Live: {m}"),
    )

    leftover = renderer.exclude_existing()
    if leftover:
        leftover_gb = sum(c.stat().st_size for c in leftover) / 1e9
        log(f"Live: {len(leftover)} raw chunk(s) from earlier runs in {chunks_dir} "
            f"({leftover_gb:.1f} GB) -- not part of this session, safe to delete.")
    renderer.clear_stale(root)

    # An output still sitting here means the last run never archived it
    # (crash, power loss) -- a clean stop moves both into sessions/. Their
    # segments are gone, so they can't be rebuilt: move them into sessions/
    # rather than let the first refresh below overwrite real footage.
    for orphan, kind in ((session_file, ""), (last_hour, "_lasthour")):
        if not orphan.exists():
            continue
        os.makedirs(sessions_dir, exist_ok=True)
        stamp = dt.datetime.fromtimestamp(orphan.stat().st_mtime)
        recovered = sessions_dir / f"{camera.name}_{stamp:%Y-%m-%d_%Hh%M}{kind}_recovered.mp4"
        try:
            os.replace(orphan, recovered)
            log(f"Live: kept the previous run's unfinished {orphan.name} as {recovered.name}")
        except OSError as e:
            log(f"Live: couldn't preserve the previous {orphan.name}: {e}")

    # last_hour.mp4 must keep spanning a rotation boundary, so the newest
    # segments are tracked separately from the block they belong to. They
    # stay on disk after their block closes, but are deliberately NOT
    # carried into the next block -- that footage is already in the closed
    # block's video, and seeding it again would duplicate it.
    recent: deque = deque(maxlen=window_chunks)

    def prune_segments() -> None:
        """Drop any segment that is neither in the open block nor still
        needed by the last_hour window."""
        keep = {s.resolve() for s in renderer.segments}
        keep |= {s.resolve() for s in recent}
        for path in segments_dir.glob("*_tl.mp4"):
            if path.resolve() not in keep:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def close_block(reason: str) -> None:
        """Finish the open block: write it out, rename it into sessions/,
        and start an empty one. The rename is why session.mp4 *is* the
        block in progress -- closing it costs no re-mux."""
        if not renderer.segments:
            return
        os.makedirs(sessions_dir, exist_ok=True)
        try:
            refresh_output(renderer.segments, session_file)
            target = sessions_dir / _block_filename(camera.name, renderer.segments)
            os.replace(session_file, target)
            log(f"Live: session complete ({reason}) -- saved {len(renderer.segments)} "
                f"chunks to {target.name}")
        except Exception as e:
            log(f"Live: couldn't close the session block: {e}")
            return
        renderer.segments = []
        prune_segments()

    def archive_last_hour() -> None:
        """On stop, keep the final window view too: the next start's first
        refresh would otherwise overwrite last_hour.mp4 with new footage."""
        if not recent or not last_hour.exists():
            return
        os.makedirs(sessions_dir, exist_ok=True)
        target = sessions_dir / _block_filename(camera.name, list(recent), "_lasthour")
        try:
            os.replace(last_hour, target)
            log(f"Live: saved the final last-hour view as {target.name}")
        except OSError as e:
            log(f"Live: couldn't archive last_hour.mp4: {e}")

    def refresh_outputs(r: ChunkRenderer, final: bool) -> None:
        recent.append(r.segments[-1])
        refresh_output(list(recent), last_hour)
        # A full re-mux of the block every chunk would grow with the square
        # of its length; every 3rd chunk (~15 min) keeps it current enough
        # to watch at a quarter of the write volume, and rotation caps how
        # big any single re-mux can get. The first chunk of a block always
        # writes, so a freshly rotated session.mp4 exists straight away
        # rather than going missing until the third chunk.
        if final or len(r.segments) == 1 or len(r.segments) % SESSION_REFRESH_EVERY == 0:
            refresh_output(r.segments, session_file)
        seg_mb = r.segment_mb()
        log(f"Live: window {len(recent)}/{window_chunks} chunks, "
            f"session {len(r.segments)} chunks, {seg_mb:.0f} MB of segments")
        if status:
            status(len(r.segments), seg_mb)
        if not final and len(r.segments) >= block_chunks:
            close_block(f"{block_chunks} chunks")
        else:
            prune_segments()

    proc = start_chunk_capture(camera, chunks_dir, chunk_seconds)
    every = (f"{chunk_seconds // 60}-minute" if chunk_seconds >= 60
             else f"{chunk_seconds}-second")
    # Width-constrained, so height follows the camera's aspect ratio --
    # 1920x1080 from a 16:9 camera, 1920x1440 from a 4:3 one.
    log(f"Live timelapse started for '{camera.name}': {every} chunks at "
        f"{speedup:g}x speed, {LIVE_OUTPUT_WIDTH}px-wide {LIVE_OUTPUT_FPS}fps output. "
        f"Outputs: {last_hour} and {session_file}. "
        f"Raw chunks are deleted once converted.")

    while not stop_event.is_set():
        if proc.poll() is not None:
            log(f"Live: capture for '{camera.name}' exited unexpectedly; retrying in "
                f"{CAPTURE_RETRY_SECONDS}s. ffmpeg said:\n{read_capture_stderr(proc)}")
            renderer.process(proc, include_newest=True, on_segment=refresh_outputs)
            if stop_event.wait(CAPTURE_RETRY_SECONDS):
                break
            proc = start_chunk_capture(camera, chunks_dir, chunk_seconds)
        renderer.process(proc, on_segment=refresh_outputs)
        stop_event.wait(2)

    stop_capture_process(proc)
    # final=True: convert the last partial chunk and bring session.mp4 fully
    # up to date regardless of where the refresh throttle landed.
    renderer.process(proc, include_newest=True, on_segment=refresh_outputs, final=True)
    # Close the partial block too, so stopping never leaves footage sitting
    # in a session.mp4 that the next run would overwrite.
    close_block("stopped")
    archive_last_hour()
    log(f"Live timelapse for '{camera.name}' stopped."
        + (f" {renderer.failed} chunk(s) failed to convert and were kept."
           if renderer.failed else ""))
