"""Lifecycle helpers for the ffmpeg capture process.

Capture itself lives in chunks.py -- this module owns the two things every
long-running ffmpeg needs, kept here so both the live view and scheduled
recordings share one implementation.

- **stderr is drained continuously** into a small bounded buffer by a
  background thread. Without the drain, any sustained stderr output
  (progress stats, decoder warnings) eventually fills the ~64KB pipe
  buffer and ffmpeg blocks mid-write -- capture silently freezes with the
  process still "running". This was the blocker for day-long runs. The
  drained tail is what the scheduler shows when ffmpeg dies unexpectedly.
- **Stop is graceful-first**: ffmpeg's 'q' command via stdin lets it finish
  writing the current file, so the last chunk of a session is never a
  truncated video that then breaks the session's final concat. Only if
  that times out do we fall back to terminate -> kill (on Windows,
  terminate is an instant kill mid-write).
"""

from __future__ import annotations

import subprocess

STDERR_TAIL_LINES = 50
GRACEFUL_STOP_TIMEOUT = 5


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
