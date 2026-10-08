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
from .decode import intel_render_node
from .rtsp import build_rtsp_url, check_ffmpeg, no_console_kwargs

CHUNK_SECONDS = 300
KEYFRAME_SNAP_MIN_INTERVAL = 5  # seconds; at/above this, keep keyframes only

# ffmpeg's deflicker filter averages brightness over a sliding window of
# this many frames (its own default). Each chunk is a separate ffmpeg run,
# so without help the window starts empty at every chunk boundary and the
# first frames of each segment get no smoothing against the previous
# chunk's brightness -- visible as a step at chunk seams while the light is
# changing fast (dusk/dawn). The fix: prime the window by decoding the
# tail of the *previous* chunk first (enough footage to fill the window),
# then trim those warm-up frames back out of the output. See
# convert_chunk's primer parameter.
DEFLICKER_SIZE = 5

# Consecutive chunks a hardware decoder may fail (each one retried in
# software) before ChunkRenderer gives up on it for the session.
HW_FAILURE_LIMIT = 3

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
                  hw_decoder: Optional[str] = None,
                  primer: Optional[Path] = None,
                  crf: int = 23) -> Path:
    """One raw chunk -> one sped-up mp4 segment.

    `interval` is real seconds between kept frames -- the same meaning it
    has on a Recording. `scale_width=None` keeps the source resolution;
    the live view passes 1920 to downscale to 1080p.

    `primer`, when given, is the *previous* chunk: its last few seconds
    are decoded ahead of this chunk (concat filter) purely to fill
    deflicker's sliding window with real history before the first frames
    of this chunk arrive, then trimmed back out of the output. Without it
    deflicker starts cold at every chunk boundary and the seam can show a
    brightness step while the light is changing fast (dusk/dawn) -- the
    "smooths within a chunk, not across chunk boundaries" limit noted
    below. Priming costs ~(DEFLICKER_SIZE+1) x interval seconds of extra
    decode per chunk (~2% at live settings) and is only applied for
    spacing-selected intervals (below KEYFRAME_SNAP_MIN_INTERVAL):
    keyframe-snapped intervals would need window x interval seconds of
    warm-up -- up to half a chunk of extra decode for a 30s interval --
    for seams that are far less visible at 10 frames per chunk. A side
    benefit measured during verification: frame *spacing* is also
    continuous across the seam, where the unprimed path restarts the
    select clock at every chunk and could double-select near boundaries.
    The trim boundary is placed half an interval before the chunk start,
    inside the guaranteed selection gap; worst-case timing alignment can
    leak one near-duplicate boundary frame per chunk, which is no worse
    than the unprimed path's own seam behaviour.

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

    `hw_decoder`, when given (see decode.py), selects hardware decode:
    either a decoder name emitted as `-c:v` (Windows NVDEC cuvid, Pi 4
    V4L2 M2M) or a "hwaccel:<method>" spec emitted as `-hwaccel`
    ("hwaccel:drm" = the Pi 5's HEVC-only rpivid block via the V4L2
    stateless request API). rpivid was measured on the real Pi 5 against
    a real production 4K chunk (2026-08-24): 3.24x-realtime decode at
    ~6.5x less CPU than software (6.6s vs 43.1s CPU per 30s of footage)
    -- transformative on a board that otherwise spends ~97% of its time
    converting. But speed is not correctness: NVDEC also ran fine while
    corrupting this camera family's nonconforming HEVC stream, and
    hardware decoders in general are less tolerant of stream quirks
    than software ones, so every hardware path -- cuvid, v4l2m2m, and
    rpivid alike -- is opt-in only (Camera.decode_mode == "hardware")
    and meant to be validated per-camera with `selftest-decode` first.

    Output encoding is software libx264 (H.264), CRF 23 -- explicit, not
    just the library default, after a real back-and-forth on 2026-08-19.
    HEVC (libx265) was tried first: benchmarked against H.264/AV1 across
    three real daylight captures, won consistently on paper (43-73% of
    H.264's size for ~100% of its encode time, SSIM ~0.96 against the
    H.264 baseline every time) and briefly shipped as the default. Real
    production use then surfaced a real, localized defect the benchmark's
    SSIM check never caught: dark/noisy low-light regions (grass at dusk)
    came out visibly smoothed/waxy under HEVC's default CRF 28, because
    HEVC's stronger prediction treats fine sensor grain as compressible
    redundancy more aggressively than H.264 does. A controlled CRF sweep
    on the same real source (HEVC crf 28/24/22/20/17 vs the H.264
    baseline) found the actual shape of the tradeoff: crf 24 was the
    *last* point where HEVC still beat H.264's size (71%), and even there
    the grain was only partially restored; crf 22 was already 113% of
    H.264's size, i.e. HEVC had already lost its size advantage before
    fully regaining H.264's quality. There is no CRF on this content where
    HEVC is simultaneously smaller than H.264 AND matches its grain
    retention -- so the honest call was to drop HEVC and go back to
    H.264, with the CRF now pinned explicitly (23, matching libx264's own
    default) instead of left implicit, so this decision is legible in the
    command line itself and doesn't silently drift if ffmpeg's own
    default ever changes. The `crf` parameter makes it per-camera
    (Camera.crf, default 23; GUI/CLI offer 20/23/26/28) -- lower is
    better quality and larger files.
    """
    ffmpeg_bin = check_ffmpeg()
    os.makedirs(segments_dir, exist_ok=True)
    out = Path(segments_dir) / f"{chunk.stem}_tl.mp4"
    tmp = Path(segments_dir) / f"{chunk.stem}_tl.tmp.mp4"

    use_primer = (primer is not None and Path(primer).exists()
                  and interval < KEYFRAME_SNAP_MIN_INTERVAL)

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
    # With a primer it also smooths across the chunk boundary; without one
    # it can only smooth within the chunk.
    vaapi = hw_decoder == "vaapi"
    if vaapi:
        # Frames are still GPU surfaces here (see decoder_flags below):
        # select only reads their timestamps/picture type, so it runs on
        # the GPU stream as-is; then scale (or just normalise to 8-bit
        # nv12) on the GPU and download only the frames that were kept.
        # Everything after this point is the same CPU graph as software.
        if scale_width:
            stages.append(f"scale_vaapi=w={scale_width}:h=-2:format=nv12")
        else:
            stages.append("scale_vaapi=format=nv12")
        stages += ["hwdownload", "format=nv12"]
    stages.append("deflicker")
    if use_primer:
        # Drop the warm-up frames now that deflicker has consumed them.
        # The boundary sits half an interval before this chunk's first
        # frame -- inside the gap the select spacing guarantees between
        # the last primer frame and the first kept frame of this chunk.
        primer_seconds = (DEFLICKER_SIZE + 1) * interval
        stages.append(f"trim=start={primer_seconds - interval / 2}")
    if scale_width and not vaapi:
        stages.append(f"scale={scale_width}:-2")
    stages.append(f"setpts=N/({output_fps}*TB)")

    # hw_decoder is either a plain decoder name ("hevc_cuvid",
    # "hevc_v4l2m2m") emitted as -c:v, or a "hwaccel:<method>" spec
    # ("hwaccel:drm" = Pi 5 rpivid) emitted as -hwaccel -- see decode.py.
    # -hwaccel is advisory: if it can't engage, ffmpeg quietly decodes in
    # software instead of erroring.
    # "vaapi" (Intel iGPU) is the third kind: -hwaccel plus
    # -hwaccel_output_format so decoded frames stay on the GPU for the
    # scale_vaapi stage above -- unlike plain -hwaccel, NOT advisory: if
    # VAAPI can't engage, the graph can't be built and ffmpeg fails
    # (ChunkRenderer then retries the chunk in software).
    global_flags = []
    if vaapi:
        global_flags = ["-init_hw_device", f"vaapi=va:{intel_render_node() or '/dev/dri/renderD128'}",
                        "-filter_hw_device", "va"]
        decoder_flags = ["-hwaccel", "vaapi", "-hwaccel_device", "va",
                         "-hwaccel_output_format", "vaapi"]
    elif hw_decoder and hw_decoder.startswith("hwaccel:"):
        decoder_flags = ["-hwaccel", hw_decoder.split(":", 1)[1]]
    elif hw_decoder:
        decoder_flags = ["-c:v", hw_decoder]
    else:
        decoder_flags = []
    # Keyframe-snapped intervals keep nothing but keyframes, so tell the
    # decoder to skip everything else instead of decoding every frame and
    # letting select throw ~49 of every 50 away. Measured 2026-10-07 on
    # the N95 mini PC against 90s of real daytime 4K pulled from the
    # camera directly (25 fps, keyframe every 2s -- via the NVR it's every
    # 4s, so even more is skipped): 80.2s -> 10.0s CPU for the full convert pipeline, kept
    # frames bit-identical (framemd5). Of 45 keyframes compared directly,
    # the only ones that differed were already damaged by lost RTSP data
    # (both decodes showing the same broken block). Not for shorter
    # intervals: those select non-key frames too.
    skip_flags = ["-skip_frame", "nokey"] if interval >= KEYFRAME_SNAP_MIN_INTERVAL else []
    decode_flags = ["-fflags", "discardcorrupt", *skip_flags, *decoder_flags]
    if use_primer:
        inputs = [
            # -sseof: decode only the primer's tail, from the nearest
            # keyframe -- a few seconds of extra decode, not a whole chunk.
            *decode_flags, "-sseof", f"-{primer_seconds}", "-i", str(primer),
            *decode_flags, "-i", str(chunk),
        ]
        graph = ["-filter_complex",
                 "[0:v][1:v]concat=n=2:v=1:a=0," + ",".join(stages) + "[out]",
                 "-map", "[out]"]
    else:
        inputs = [*decode_flags, "-i", str(chunk)]
        graph = ["-vf", ",".join(stages)]

    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error", "-nostats",
        *global_flags, *inputs,
        # -r is load-bearing, not redundant with setpts: without it ffmpeg
        # derives the output rate from the input stream's metadata (12.5
        # fps on this camera) and DROPS frames to match -- measured 11 of
        # 47 kept. setpts sets the timestamps; -r sets the output rate.
        "-an", *graph, "-r", str(output_fps),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
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
                 hw_decoder: Optional[str] = None, crf: int = 23,
                 log: Callable[[str], None] = print):
        self.chunks_dir = Path(chunks_dir)
        self.segments_dir = Path(segments_dir)
        self.interval = interval
        self.output_fps = output_fps
        self.scale_width = scale_width
        self.hw_decoder = hw_decoder
        self.crf = crf
        self.log = log
        self.segments: List[Path] = []
        self.processed: set = set()
        self.failed = 0
        # Hardware-decode failures in a row; the decoder is only dropped
        # for the session at HW_FAILURE_LIMIT (see process()).
        self.hw_failures = 0
        # The most recent chunk, retained one extra cycle to prime the next
        # conversion's deflicker window (see convert_chunk). _pending_delete
        # is the successfully converted chunk awaiting deletion once its
        # successor has used it -- failed chunks are never queued here, so
        # the keep-for-diagnosis guarantee is untouched.
        self._primer: Optional[Path] = None
        self._pending_delete: Optional[Path] = None

    def exclude_existing(self) -> List[Path]:
        """Mark chunks already on disk as handled and return them.

        A session means "since Start". Chunks left by an earlier run sort
        first by name and would otherwise prepend old footage. The engine
        itself never touches them -- they're returned for the caller to
        report or clean up (the live view deletes them as crash leftovers;
        see run_live).
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

    def _convert(self, chunk: Path, hw_decoder: Optional[str],
                 primer: Optional[Path]) -> Path:
        return convert_chunk(chunk, self.segments_dir, interval=self.interval,
                             output_fps=self.output_fps, scale_width=self.scale_width,
                             hw_decoder=hw_decoder, primer=primer, crf=self.crf)

    def _convert_with_fallbacks(self, chunk: Path) -> Path:
        """convert_chunk, degrading step by step instead of failing.

        Neither optional input may take a chunk down with it: a damaged
        primer must not cascade, and a hardware decoder that fails at
        runtime must not fail the session (real case: a Pi 5's ffmpeg
        lists h264/hevc_v4l2m2m but the board has no decode block, so the
        decoder can't open). With both in play the hardware decoder is
        retried without the primer first: Intel VAAPI gives up on damaged
        data wherever it is, including in the previous chunk's tail that
        the primer re-decodes -- seen for real (2026-10-07): one damaged
        chunk then failed the clean chunk after it, and each software
        redo costs a small box minutes of every core it's allowed. Last
        resort is plain software decode with no primer.
        """
        attempts = [(self.hw_decoder, self._primer)]
        if self.hw_decoder is not None and self._primer is not None:
            attempts.append((self.hw_decoder, None))
        if self.hw_decoder is not None or self._primer is not None:
            attempts.append((None, None))
        for i, (hw, primer) in enumerate(attempts):
            try:
                seg = self._convert(chunk, hw, primer)
            except Exception:
                if i == len(attempts) - 1:
                    raise
                nxt_hw, nxt_primer = attempts[i + 1]
                dropped = [n for n, before, after in (
                    ("deflicker primer", primer, nxt_primer),
                    (f"hardware decoder '{hw}'", hw, nxt_hw)) if before and not after]
                self.log(f"Converting {chunk.name} failed; retrying without "
                         f"{' or '.join(dropped)}.")
                continue
            if hw is not None:
                self.hw_failures = 0
            elif self.hw_decoder is not None:
                self._note_hw_failure(chunk)
            return seg
        raise AssertionError("unreachable")

    def _note_hw_failure(self, chunk: Path) -> None:
        """The chunk converted only in software. One failure isn't proof
        the decoder can't work: Intel VAAPI decodes this camera fine but
        gives up on a chunk with damaged data (lost RTSP packets), and
        dropping it for the session over that would put a small box on
        full software decode for good. Several in a row is a decoder that
        doesn't work here -- stop paying a failed attempt on every chunk."""
        self.hw_failures += 1
        if self.hw_failures >= HW_FAILURE_LIMIT:
            self.log(f"Hardware decoder '{self.hw_decoder}' failed {self.hw_failures} "
                     f"chunks in a row; using software decode for the rest of this "
                     f"session.")
            self.hw_decoder = None
        else:
            self.log(f"Hardware decoder '{self.hw_decoder}' failed on {chunk.name} "
                     f"(damaged footage?); converted it in software, keeping hardware "
                     f"decode for the next chunk.")

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
                    seg = self._convert_with_fallbacks(chunk)
            except Exception as e:
                self.failed += 1
                self._primer = chunk  # still real adjacent footage for the next seam
                self.log(f"Converting {chunk.name} failed ({e}); raw chunk kept for "
                         f"diagnosis ({self.failed} kept so far this session).")
                continue
            self.segments.append(seg)
            made += 1
            # The converted chunk is retained one cycle as the next chunk's
            # deflicker primer, then deleted -- deletion is deferred, never
            # gated on an output refresh. (Gating it on the refresh once
            # orphaned a chunk permanently whenever an output file was
            # locked by a video player. Seen in the wild: 2.6 GB in one
            # session.) Peak disk is one extra chunk over the old
            # delete-immediately behaviour.
            if self._pending_delete is not None:
                try:
                    os.remove(self._pending_delete)
                except OSError as e:
                    self.log(f"Couldn't delete converted chunk "
                             f"{self._pending_delete.name}: {e}")
            self._primer = chunk
            self._pending_delete = chunk
            if on_segment is not None:
                try:
                    on_segment(self, final)
                except Exception as e:
                    self.log(f"Updating outputs failed: {e}")
        if final and self._pending_delete is not None:
            # Session over -- nothing left to prime, so the last retained
            # chunk can go too. Without this it would linger and be
            # reported as leftover at the next start.
            try:
                os.remove(self._pending_delete)
            except OSError as e:
                self.log(f"Couldn't delete converted chunk "
                         f"{self._pending_delete.name}: {e}")
            self._pending_delete = None
            self._primer = None
        return made
