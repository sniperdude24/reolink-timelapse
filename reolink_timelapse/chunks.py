"""Shared video-chunk capture engine.

One ffmpeg stream-copies the camera's RTSP feed into gapless timestamped
chunk files (no decode, no encode -- near-zero CPU while capturing). Each
completed chunk is then rendered into a small sped-up segment, and the raw
chunk is deleted. Finished videos are concat-remuxes of segments (-c copy,
cheap), so a long recording never pays one enormous render at the end --
the work is spread across the session as it runs. That incremental
property is load-bearing, not an optimisation: a 14-hour session rendered
in one lump would decode ~14h of 4K at ~6.7x realtime, about two hours of
work at the moment the session closes.

Both capture paths use this engine:

- the Live Timelapse panel (see live.py) -- rolling window outputs
- scheduled recordings (see scheduler.py) -- one final video per session

Chunks use the mpegts container so a crash mid-write still leaves a
playable file, and are named by wall-clock start time so they sort
chronologically across capture restarts.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, List, Optional

from .capture import STDERR_TAIL_LINES, _drain_stderr
from .rtsp import build_rtsp_url, check_ffmpeg, no_console_kwargs

CHUNK_SECONDS = 300
KEYFRAME_SNAP_MIN_INTERVAL = 5  # seconds; at/above this, keep keyframes only

# Rendering is the only expensive step, and it arrives in bursts: ~55s at
# ~1.7 cores (peak 3.5) for each 5-minute 4K chunk. Capturing is nearly
# free, so N cameras cost ~N x 0.31 cores on average -- but if their chunks
# close together the bursts stack and briefly swamp the machine. One global
# lock makes renders queue instead. Three 4K cameras need ~165s of render
# per 300s window, so serialising still keeps up comfortably.
_RENDER_LOCK = threading.Lock()


def start_chunk_capture(source, chunks_dir: Path,
                        chunk_seconds: int = CHUNK_SECONDS) -> subprocess.Popen:
    """Stream-copy the feed into `chunk_seconds` .ts files.

    `source` is anything carrying the RTSP source fields -- a Camera (live
    view) or a Setup (scheduled recording); build_rtsp_url reads the same
    attributes from either.
    """
    ffmpeg_bin = check_ffmpeg()
    os.makedirs(chunks_dir, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-loglevel", "error", "-nostats",
        "-rtsp_transport", "tcp",
        "-timeout", "10000000",
        "-i", build_rtsp_url(source),
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


def convert_chunk(chunk: Path, segments_dir: Path, *, interval: float,
                  output_fps: float, scale_width: Optional[int] = None,
                  hw_decoder: Optional[str] = None) -> Path:
    """One raw chunk -> one sped-up mp4 segment.

    `interval` is real seconds between kept frames -- the same meaning it
    has on a Recording. `scale_width=None` keeps the source resolution;
    the live view passes 1920 to downscale to 1080p.

    Intervals of KEYFRAME_SNAP_MIN_INTERVAL or more keep only keyframes.
    A lost slice corrupts every following frame until the next keyframe
    (measured GOP here: 3.91s), so keyframes are the only frames immune to
    that propagation -- the same mitigation the old JPEG path used, where
    it cut visibly damaged saves from 75/176 to 0/9. Shorter intervals
    can't use it (too few keyframes to hit the target spacing) and fall
    back to spacing alone.

    Decode is software by default: no -hwaccel. GPU decode (NVDEC) on
    Windows was rejected after real measurement, and re-confirmed against
    *this* pipeline specifically -- not just carried forward from before
    the JPEG-to-chunk rewrite. Original finding (2026-08-16, old
    JPEG-capture path): on this camera family's nonconforming tiled HEVC
    (PPS re-sent mid-frame), NVDEC mis-stitches tile boundaries into a
    vivid-green vertical line -- decoding the same recorded 10-minute
    stream twice gave 0 line frames in software vs 401 with NVDEC.
    Re-test (2026-08-18, via `selftest-decode` against this exact
    convert_chunk() pipeline, real 5-minute NVR capture): 21 of 287
    frames differed significantly (SSIM) between software and
    `hevc_cuvid` decode -- the corruption is real and reproducible on the
    current pipeline, not a stale conclusion. The same re-test against
    this camera family's H.264 stream (via NVR, `h264_cuvid`), by
    contrast, came back clean (0 of 173 frames) on a 3-minute capture --
    a genuinely new result the original investigation never covered,
    suggesting the corruption is specific to this camera's nonconforming
    HEVC encoding rather than NVDEC in general. Neither finding is a
    reason to change the default: HEVC stays off given the reproduced
    corruption, and one clean H.264 sample isn't enough runway to trust
    hardware decode there either -- both remain opt-in only. NVENC
    *encoding* was also measured and rejected: encode is only ~9% of
    conversion cost, so it saves ~5% CPU while making files 4x larger.

    `hw_decoder`, when given (see decode.py), forces a specific decoder.
    On Windows this now includes NVDEC (`h264_cuvid`/`hevc_cuvid`) per
    the re-test above, alongside Raspberry Pi's V4L2 M2M hardware blocks
    -- a different decoder IP entirely, so the NVDEC finding doesn't
    necessarily transfer to it. It might, though: hardware decoders are
    often less tolerant of nonconforming streams than software ones, and
    nobody has run the same kind of A/B measurement against real Pi
    hardware yet (that's what the `selftest-decode` CLI command is for,
    on either platform). Treat Pi hardware decode as experimental until
    that's been done -- it stays off (None) unless a Camera's
    decode_mode is explicitly set to "hardware" and decode.py's
    capability probe found a matching decoder.
    """
    ffmpeg_bin = check_ffmpeg()
    os.makedirs(segments_dir, exist_ok=True)
    out = Path(segments_dir) / f"{chunk.stem}_tl.mp4"
    tmp = Path(segments_dir) / f"{chunk.stem}_tl.tmp.mp4"

    # \, -- comma is a filter-graph separator and must be escaped inside
    # the select expression.
    spacing = f"isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval})"
    if interval >= KEYFRAME_SNAP_MIN_INTERVAL:
        stages = [f"select=eq(pict_type\\,I)*({spacing})"]
    else:
        stages = [f"select={spacing}"]
    # deflicker evens out the brightness jitter between kept frames -- most
    # visible across dawn/dusk, when the camera is changing exposure (and
    # halving its frame rate) between one kept frame and the next. It runs
    # after select so it only sees frames that survive into the video.
    # Known limit: it smooths within a chunk, not across chunk boundaries.
    stages.append("deflicker")
    if scale_width:
        stages.append(f"scale={scale_width}:-2")
    stages.append(f"setpts=N/({output_fps}*TB)")

    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error", "-nostats",
        "-fflags", "discardcorrupt",
        *(["-c:v", hw_decoder] if hw_decoder else []),
        "-i", str(chunk),
        # -r is load-bearing, not redundant with setpts: without it ffmpeg
        # derives the output rate from the input stream's metadata (12.5
        # fps on this camera) and DROPS frames to match -- measured 11 of
        # 47 kept. setpts sets the timestamps; -r sets the output rate.
        "-an", "-vf", ",".join(stages), "-r", str(output_fps),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, **no_console_kwargs())
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError((r.stderr or "").strip()[-300:] or f"ffmpeg exited {r.returncode}")
    os.replace(tmp, out)
    return out


def _replace_with_retry(tmp: Path, out_path: Path, attempts: int = 5) -> None:
    """os.replace, retried briefly.

    On Windows the replace fails with PermissionError while the target is
    open in another process -- exactly what happens when you're watching
    an output file in a player as the next chunk lands. The lock clears as
    soon as the player releases the file, so a few short retries turn a
    hard failure into a hiccup.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, out_path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.4)


def refresh_output(segments: List[Path], out_path: Path) -> None:
    """Concat-remux segments into out_path, atomically (temp + replace)."""
    if not segments:
        return
    ffmpeg_bin = check_ffmpeg()
    out_path = Path(out_path)
    list_path = out_path.with_suffix(".list.txt")
    tmp = out_path.with_suffix(".new.mp4")
    os.makedirs(out_path.parent, exist_ok=True)
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
    _replace_with_retry(tmp, out_path)


class ChunkRenderer:
    """Turns completed chunks into segments, one session's worth.

    Callers drive their own outer loop (the live view runs until stopped;
    the scheduler runs to a window end and retries ffmpeg itself) and call
    process() periodically. Everything session-scoped -- which chunks are
    already handled, the segments built so far, the failure count -- lives
    here.
    """

    def __init__(self, chunks_dir: Path, segments_dir: Path, *, interval: float,
                 output_fps: float, scale_width: Optional[int] = None,
                 hw_decoder: Optional[str] = None,
                 log: Callable[[str], None] = print):
        self.chunks_dir = Path(chunks_dir)
        self.segments_dir = Path(segments_dir)
        self.interval = interval
        self.output_fps = output_fps
        self.scale_width = scale_width
        self.hw_decoder = hw_decoder
        self.log = log
        self.segments: List[Path] = []
        self.processed: set = set()
        self.failed = 0

    def exclude_existing(self) -> List[Path]:
        """Mark chunks already on disk as handled and return them.

        A session means "since Start". Chunks left by an earlier run sort
        first by name and would otherwise prepend old footage. They are the
        user's to delete, so report them but never touch them.
        """
        leftover = sorted(self.chunks_dir.glob("*.ts"))
        self.processed.update(c.name for c in leftover)
        return leftover

    def clear_stale(self, *roots: Path) -> None:
        """Delete a previous session's segments and any half-written temp
        files a crash left behind (they match neither the segment glob nor
        anything else that cleans up)."""
        stale = (list(self.segments_dir.glob("*_tl.mp4"))
                 + list(self.segments_dir.glob("*_tl.tmp.mp4")))
        for root in roots:
            stale += list(Path(root).glob("*.new.mp4"))
            stale += list(Path(root).glob("*.list.txt"))
        for path in stale:
            try:
                os.remove(path)
            except OSError:
                pass

    def segment_mb(self) -> float:
        return sum(s.stat().st_size for s in self.segments if s.exists()) / 1e6

    def process(self, proc: Optional[subprocess.Popen], *, include_newest: bool = False,
                on_segment: Optional[Callable[["ChunkRenderer", bool], None]] = None,
                final: bool = False) -> int:
        """Render every completed chunk not yet handled. Returns how many
        new segments were produced.

        `on_segment(renderer, final)` runs after each successful chunk so
        the caller can refresh whatever outputs it maintains; it is called
        inside a try/except, since a failed refresh must never abort the
        loop or strand a chunk.
        """
        chunks = sorted(self.chunks_dir.glob("*.ts"))
        if not include_newest and proc is not None and proc.poll() is None:
            chunks = chunks[:-1]  # newest is still being written to
        made = 0
        for chunk in chunks:
            if chunk.name in self.processed:
                continue
            self.processed.add(chunk.name)
            try:
                # Serialised across every camera -- see _RENDER_LOCK.
                with _RENDER_LOCK:
                    seg = convert_chunk(chunk, self.segments_dir, interval=self.interval,
                                        output_fps=self.output_fps,
                                        scale_width=self.scale_width,
                                        hw_decoder=self.hw_decoder)
            except Exception as e:
                self.failed += 1
                self.log(f"Converting {chunk.name} failed ({e}); raw chunk kept for "
                         f"diagnosis ({self.failed} kept so far this session).")
                continue
            self.segments.append(seg)
            made += 1
            # Delete as soon as the segment exists: the segment is the
            # durable artifact and any outputs are derived from it.
            # (Deleting after the refresh instead meant a locked output
            # file -- e.g. one open in a video player -- orphaned the raw
            # chunk permanently. Seen in the wild: 2.6 GB in one session.)
            try:
                os.remove(chunk)
            except OSError as e:
                self.log(f"Couldn't delete converted chunk {chunk.name}: {e}")
            if on_segment is not None:
                try:
                    on_segment(self, final)
                except Exception as e:
                    self.log(f"Updating outputs failed: {e}")
        return made
