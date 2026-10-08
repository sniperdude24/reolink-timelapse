"""Hardware-decode capability detection for chunk conversion.

Every camera is decoded in software by default (see chunks.py's
convert_chunk docstring for the full history and numbers). GPU decode on
Windows (NVDEC) was rejected in 2026-08-16 against the old JPEG-capture
pipeline, then actually re-tested on 2026-08-18 against this exact
chunk-based pipeline via `selftest-decode` on real NVR footage: the HEVC
corruption reproduced (21/287 frames), so it stays rejected -- now
re-confirmed, not just inherited. This camera family's H.264 stream via
NVDEC came back clean in the same round of testing, a question the
original investigation never asked -- still opt-in only pending more
runway on that result. Raspberry Pi 4 has a different hardware decoder
block (V4L2 M2M, not NVDEC), which might avoid HEVC's failure mode or
might not -- nobody has tested it on real hardware yet. Everything here
is written to *default to software* and only offer hardware decode as
an explicit, self-tested opt-in (Camera.decode_mode == "hardware"),
never a silent assumption -- true for NVDEC and V4L2 M2M alike: both are
opt-in, and both are meant to be self-verified with `selftest-decode`
before being trusted, regardless of which one your platform offers.

Every function in this module fails closed: any probing hiccup --
ffmpeg missing, stream unreachable, unrecognized output -- resolves to
"use software", never raises up into a capture loop that has to keep
running regardless.
"""

from __future__ import annotations

import platform
import re
import subprocess
import sys
from typing import Optional

from .rtsp import build_rtsp_url, no_console_kwargs

# ffmpeg decode-selection specs per hardware-decode family this project
# knows about, dispatched by platform + device probing. A spec is either
# a plain ffmpeg decoder name (emitted as `-c:v <name>`) or
# "hwaccel:<method>" (emitted as `-hwaccel <method>`, keeping ffmpeg's
# normal decoder but accelerating it).
#
# Windows: NVDEC via ffmpeg's cuvid decoders. Raspberry Pi 4: general
# V4L2 M2M blocks for both codecs. Raspberry Pi 5: the Pi 4's M2M blocks
# are GONE (hevc/h264_v4l2m2m pass feature detection because they're
# compiled in, then fail at open with "Could not find a valid device" --
# learned on real hardware 2026-08-22), and there is no hardware encoder
# and no H.264 decode of any kind -- but it DOES have a dedicated
# HEVC-only decode block, "rpivid" (/dev/video19, driver rpi-hevc-dec),
# exposed through the V4L2 *stateless request* API. Raspberry Pi OS's
# patched ffmpeg (--enable-v4l2-request --enable-sand) drives it via
# `-hwaccel drm`. Measured on the real Pi 5 against a real production 4K
# chunk (2026-08-24): software decode 43.1s CPU / 33.3s wall per 30s of
# footage vs 6.6s CPU / 9.3s wall with -hwaccel drm -- 3.24x realtime,
# ~6.5x less CPU, all frames decoded. Speed is not correctness, though:
# NVDEC also ran fine while corrupting 401 frames of this camera
# family's nonconforming HEVC stream, so rpivid gets the same treatment
# as every hardware path -- opt-in only, validated per-camera with
# `selftest-decode` before being trusted.
#
# Runtime failure of any spec is handled where it happens: selftest-
# decode reports it as its verdict, and ChunkRenderer retries in
# software and latches hardware off for the session. (For "hwaccel:"
# specs that latch rarely fires -- -hwaccel is advisory, so ffmpeg
# falls back to software decode by itself instead of erroring.)
#
# x86 Linux with an Intel iGPU (i915/xe driver -- e.g. an N95/N100 mini
# PC): VAAPI through Intel's iHD media driver, spec "vaapi". Unlike the
# "hwaccel:" specs this one keeps frames ON the GPU (-hwaccel_output_
# format vaapi) so selection and the 4K->1080p scale happen there and
# only kept frames are downloaded -- convert_chunk builds a different
# filter graph for it. Measured on the N95 against a real 5-minute 4K
# chunk (2026-10-07): 36s CPU for the full live pipeline vs ~6 core-
# minutes in software, and the frames it decodes are pixel-identical to
# software (SSIM 1.000) on this camera family's tiled HEVC -- the stream
# NVDEC corrupts. Its failure mode is different: a chunk with damaged
# data (lost RTSP packets) makes the GPU hang (the kernel resets just
# that context) and ffmpeg exits non-zero partway through, so
# ChunkRenderer redoes that one chunk in software.
_NVDEC_DECODERS = {"h264": "h264_cuvid", "hevc": "hevc_cuvid"}
_PI_V4L2_DECODERS = {"h264": "h264_v4l2m2m", "hevc": "hevc_v4l2m2m"}
_PI5_RPIVID = {"hevc": "hwaccel:drm"}
_INTEL_VAAPI = {"h264": "vaapi", "hevc": "vaapi"}

# Kernel drivers of Intel GPUs whose video engine iHD drives.
_INTEL_DRM_DRIVERS = ("i915", "xe")


def intel_render_node() -> Optional[str]:
    """/dev/dri/renderD* of the first Intel GPU, or None. Device probing
    via sysfs, no ffmpeg needed; fails closed on any filesystem hiccup."""
    try:
        from pathlib import Path
        for node in sorted(Path("/sys/class/drm").glob("renderD*")):
            driver = (node / "device" / "driver").resolve().name
            if driver in _INTEL_DRM_DRIVERS and Path("/dev/dri", node.name).exists():
                return f"/dev/dri/{node.name}"
    except OSError:
        pass
    return None


def _pi5_rpivid_present() -> bool:
    """Whether this host exposes the Pi 5's rpivid HEVC decode block
    (V4L2 device named rpi-hevc-dec). Device probing, no ffmpeg needed;
    fails closed on any filesystem hiccup."""
    try:
        from pathlib import Path
        for name_file in Path("/sys/class/video4linux").glob("*/name"):
            if "rpi-hevc-dec" in name_file.read_text(errors="ignore"):
                return True
    except OSError:
        pass
    return False


def decoder_map_for_platform() -> dict:
    """Which hardware-decode family applies to this host, if any -- pure
    host-capability dispatch, says nothing about whether hardware decode
    is actually safe for a given camera's stream (that's what a decode
    self-test is for)."""
    if sys.platform == "win32":
        return _NVDEC_DECODERS
    if sys.platform == "linux" and platform.machine() in (
        "aarch64", "armv7l", "arm64",
    ):
        # Pi 5: rpivid handles HEVC only; nothing exists for H.264.
        # Anything else ARM/Linux falls back to the Pi 4 M2M family.
        if _pi5_rpivid_present():
            return _PI5_RPIVID
        return _PI_V4L2_DECODERS
    if sys.platform == "linux" and intel_render_node() is not None:
        return _INTEL_VAAPI
    return {}


def hw_mechanism_name() -> str:
    """Human name of this host's hardware-decode family, for prompts."""
    family = decoder_map_for_platform()
    if family is _NVDEC_DECODERS:
        return "NVDEC"
    if family is _INTEL_VAAPI:
        return "Intel VAAPI"
    if family is _PI5_RPIVID:
        return "rpivid"
    return "V4L2 M2M"


def hw_decode_platform() -> bool:
    """Whether this host has any known hardware-decode family at all."""
    return bool(decoder_map_for_platform())


def probe_codec(source, ffmpeg_bin: str, timeout: float = 8.0) -> Optional[str]:
    """"h264"/"hevc"/None for the source's video codec, from a brief
    connect-and-read (no ffprobe dependency -- this project doesn't
    bundle it, see start_chunk_capture for the same reasoning)."""
    try:
        r = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-rtsp_transport", "tcp",
             "-timeout", str(int(timeout * 1_000_000)),
             "-i", build_rtsp_url(source), "-t", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout + 5, **no_console_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Video:\s*(h264|hevc)\b", r.stderr)
    return m.group(1) if m else None


def hw_decoders_available(ffmpeg_bin: str) -> set:
    """Which of this platform's candidate hardware-decode specs the local
    ffmpeg build actually exposes -- feature detection only (the decoder
    or hwaccel method exists), not a correctness guarantee (that it
    decodes *this* stream cleanly). Plain decoder names are checked
    against `-decoders`; "hwaccel:<method>" specs against `-hwaccels`."""
    specs = set(decoder_map_for_platform().values())
    if not specs:
        return set()
    available = set()
    listings = {}  # ffmpeg flag -> its output, fetched at most once each
    for spec in specs:
        flag = "-hwaccels" if spec.startswith("hwaccel:") or spec == "vaapi" else "-decoders"
        if flag not in listings:
            try:
                r = subprocess.run(
                    [ffmpeg_bin, "-hide_banner", flag],
                    capture_output=True, text=True, timeout=10,
                    **no_console_kwargs(),
                )
                listings[flag] = r.stdout
            except (OSError, subprocess.TimeoutExpired):
                listings[flag] = ""
        needle = spec.split(":", 1)[1] if spec.startswith("hwaccel:") else spec
        if needle in listings[flag].split():
            if spec == "hwaccel:drm" and not _ffmpeg_has_v4l2_request(ffmpeg_bin):
                # Every Linux ffmpeg lists a generic 'drm' hwaccel; only
                # Raspberry Pi's patched build (--enable-v4l2-request) can
                # actually drive rpivid through it. Seen for real in the
                # generic Docker image on a Pi host (2026-09-04): device
                # present, 'drm' listed, conversion failed outright.
                continue
            available.add(spec)
    return available


def _ffmpeg_has_v4l2_request(ffmpeg_bin: str) -> bool:
    try:
        r = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=10, **no_console_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--enable-v4l2-request" in r.stdout


def resolve_decoder(codec: Optional[str], mode: str, ffmpeg_bin: str) -> Optional[str]:
    """The ffmpeg decoder to force for `codec` under `mode`
    ("software"/"hardware"), or None to let ffmpeg pick its software
    default. Fails closed to None on any unknown codec, unsupported
    platform, or missing decoder -- never raises."""
    if mode != "hardware" or codec is None:
        return None
    decoder = decoder_map_for_platform().get(codec)
    if decoder is None or decoder not in hw_decoders_available(ffmpeg_bin):
        return None
    return decoder


def resolve_decoder_for_source(source, mode: str, ffmpeg_bin: str,
                               log=print) -> Optional[str]:
    """probe_codec + resolve_decoder in one step, for callers (live.py,
    scheduler.py) that just have a Camera/Setup and want "what decoder
    flag, if any, should this session's renderer use." Swallows anything
    unexpected -- a decode-mode probing hiccup must never stop capture."""
    if mode != "hardware":
        return None
    try:
        codec = probe_codec(source, ffmpeg_bin)
        decoder = resolve_decoder(codec, mode, ffmpeg_bin)
    except Exception as e:  # probing must never take capture down with it
        log(f"Hardware-decode probe failed ({e}); using software decode.")
        return None
    if decoder:
        log(f"Using hardware decoder '{decoder}' for {codec} (experimental, "
            f"unvalidated -- run 'selftest-decode' to check for corruption "
            f"on this camera before trusting it).")
    elif codec is not None:
        log(f"decode_mode is 'hardware' but no matching decoder is available "
            f"for {codec} on this system; using software decode.")
    return decoder
