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

# ffmpeg decoder names per hardware-decode family this project knows
# about, dispatched by platform. Windows: NVDEC via ffmpeg's cuvid
# decoders. Linux/ARM (Raspberry Pi): V4L2 M2M -- Pi 4 has both codecs'
# blocks, Pi 5 has neither (no hardware video decode block at all), which
# hw_decoders_available() naturally reflects since ffmpeg won't list
# decoders the running kernel/driver doesn't expose. Anywhere else:
# no known hardware-decode family, hardware mode always resolves to None.
_NVDEC_DECODERS = {"h264": "h264_cuvid", "hevc": "hevc_cuvid"}
_PI_V4L2_DECODERS = {"h264": "h264_v4l2m2m", "hevc": "hevc_v4l2m2m"}


def decoder_map_for_platform() -> dict:
    """Which hardware-decoder family applies to this host, if any -- pure
    host-capability dispatch, says nothing about whether hardware decode
    is actually safe for a given camera's stream (that's what a decode
    self-test is for)."""
    if sys.platform == "win32":
        return _NVDEC_DECODERS
    if sys.platform == "linux" and platform.machine() in (
        "aarch64", "armv7l", "arm64",
    ):
        return _PI_V4L2_DECODERS
    return {}


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
    """Which of this platform's candidate hardware decoders the local
    ffmpeg build actually exposes -- feature detection only (the decoder
    exists), not a correctness guarantee (that it decodes *this* stream
    cleanly)."""
    try:
        r = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-decoders"],
            capture_output=True, text=True, timeout=10, **no_console_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {name for name in decoder_map_for_platform().values() if name in r.stdout}


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
