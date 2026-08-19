"""Hardware-decode capability detection for chunk conversion.

Every camera is decoded in software today (see chunks.py's convert_chunk
docstring): GPU decode was tried on Windows and rejected after real
measurement -- NVDEC mis-stitched this camera family's nonconforming
tiled HEVC into visible corruption. Raspberry Pi 4 has a different
hardware decoder block (V4L2 M2M, not NVDEC), which might avoid that
specific failure mode or might not -- nobody has tested it on real
hardware yet. Everything here is written to *default to software* and
only offer hardware decode as an explicit, self-tested opt-in
(Camera.decode_mode == "hardware"), never a silent assumption.

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

# ffmpeg decoder names for Raspberry Pi OS's V4L2 M2M hardware blocks.
# Pi 4 has both; Pi 5 has neither (no hardware video decode block at
# all) -- hw_decoders_available() naturally returns empty there since
# ffmpeg won't list decoders the running kernel/driver doesn't expose.
HW_DECODERS = {
    "h264": "h264_v4l2m2m",
    "hevc": "hevc_v4l2m2m",
}


def hw_decode_platform() -> bool:
    """Whether this machine is even the right *kind* of hardware for the
    V4L2 M2M path -- Linux on an ARM board. Pure host-capability gating;
    says nothing about whether hardware decode is safe for a given
    camera's stream (that's what a decode self-test is for)."""
    return sys.platform == "linux" and platform.machine() in (
        "aarch64", "armv7l", "arm64",
    )


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
    """Which of HW_DECODERS' values this ffmpeg build actually exposes --
    feature detection only (the decoder exists), not a correctness
    guarantee (that it decodes *this* stream cleanly)."""
    try:
        r = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-decoders"],
            capture_output=True, text=True, timeout=10, **no_console_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {name for name in HW_DECODERS.values() if name in r.stdout}


def resolve_decoder(codec: Optional[str], mode: str, ffmpeg_bin: str) -> Optional[str]:
    """The ffmpeg decoder to force for `codec` under `mode`
    ("software"/"hardware"), or None to let ffmpeg pick its software
    default. Fails closed to None on any unknown codec, unsupported
    platform, or missing decoder -- never raises."""
    if mode != "hardware" or codec is None or not hw_decode_platform():
        return None
    decoder = HW_DECODERS.get(codec)
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
