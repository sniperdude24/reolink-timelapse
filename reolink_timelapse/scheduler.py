"""Long-running daily scheduler: captures only during the configured window.

Four schedule modes: "daylight" -- today's actual sunrise/sunset at the
setup's location (recomputed every day, so it drifts correctly with the
seasons), widened by pre/post offset minutes. "fixed_time" -- the same
daily-recurring idea, but with clock times you set instead of computed
sunrise/sunset. "always" -- no window, capture runs continuously until
stopped. "duration" -- a manual one-shot timer: capture starts as soon as
run_scheduled() is called and stops itself after schedule.duration_minutes,
with no daily repeat.

Every wait in here is chunked against a threading.Event rather than one long
blocking sleep, so a caller (the GUI, running this in a background thread)
can request a clean stop without waiting out an entire daylight window --
and so ffmpeg always gets terminated properly rather than orphaned.

Each continuous start-to-stop capture run (one daylight window, or one
manual start/stop in "always" mode) is a "session": its frames go into
their own subfolder under frames_dir, named from the session's start time
plus a short random suffix so two sessions can never collide -- including
two instances of the same setup started at once. A video is built
automatically from each session's frames right after it ends.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from astral import LocationInfo
from astral.sun import sun
from zoneinfo import ZoneInfo

from .build import build_timelapse
from .capture import read_capture_stderr, start_capture_process, stop_capture_process
from .config import Setup

RETRY_DELAY_SECONDS = 30
POLL_SECONDS = 2.0


@dataclass
class Window:
    start: dt.datetime
    end: dt.datetime


def daylight_window(setup: Setup, date: dt.date) -> Window:
    sched = setup.schedule
    tz = ZoneInfo(sched.timezone)
    loc = LocationInfo(latitude=sched.latitude, longitude=sched.longitude, timezone=sched.timezone)
    s = sun(loc.observer, date=date, tzinfo=tz)
    start = s["sunrise"] - dt.timedelta(minutes=sched.pre_offset_minutes)
    end = s["sunset"] + dt.timedelta(minutes=sched.post_offset_minutes)
    return Window(start=start, end=end)


def fixed_time_window(setup: Setup, date: dt.date) -> Window:
    sched = setup.schedule
    tz = ZoneInfo(sched.timezone) if sched.timezone else dt.datetime.now().astimezone().tzinfo
    start_t = dt.datetime.strptime(sched.start_time, "%H:%M").time()
    end_t = dt.datetime.strptime(sched.end_time, "%H:%M").time()
    start = dt.datetime.combine(date, start_t, tzinfo=tz)
    end = dt.datetime.combine(date, end_t, tzinfo=tz)
    if end <= start:
        end += dt.timedelta(days=1)  # overnight window, e.g. 22:00-02:00
    return Window(start=start, end=end)


def _now(setup: Setup) -> dt.datetime:
    if setup.schedule.timezone:
        return dt.datetime.now(ZoneInfo(setup.schedule.timezone))
    return dt.datetime.now().astimezone()


def _window_for(setup: Setup, date: dt.date) -> Window:
    if setup.schedule.mode == "fixed_time":
        return fixed_time_window(setup, date)
    return daylight_window(setup, date)


def next_window(setup: Setup) -> Window:
    """Today's (or tomorrow's, if today's has ended) window for the
    "daylight" or "fixed_time" recurring modes."""
    now = _now(setup)
    window = _window_for(setup, now.date())
    if now >= window.end:
        window = _window_for(setup, now.date() + dt.timedelta(days=1))
    return window


def _sleep_until(target: dt.datetime, setup: Setup, log, stop_event: threading.Event) -> bool:
    """Sleep in short chunks until `target`. Returns True if stopped early."""
    remaining = (target - _now(setup)).total_seconds()
    if remaining > 0:
        log(f"Sleeping until {target.strftime('%Y-%m-%d %H:%M:%S %Z')} "
            f"({remaining / 3600:.1f}h)...")
    while remaining > 0:
        if stop_event.wait(min(remaining, POLL_SECONDS)):
            return True
        remaining = (target - _now(setup)).total_seconds()
    return False


def _wait_capture(proc, window_end, setup: Setup, stop_event: threading.Event) -> str:
    """Poll the running capture process in short chunks.

    Returns "exited" (ffmpeg died on its own), "window_end" (daylight window
    is over), or "stopped" (caller requested a stop).
    """
    while True:
        if stop_event.is_set():
            return "stopped"
        if window_end is not None:
            remaining = (window_end - _now(setup)).total_seconds()
            if remaining <= 0:
                return "window_end"
            chunk = min(remaining, POLL_SECONDS)
        else:
            chunk = POLL_SECONDS
        try:
            proc.wait(timeout=chunk)
            return "exited"
        except subprocess.TimeoutExpired:
            continue


def _new_session_dir(setup: Setup, start: dt.datetime) -> str:
    session_id = f"{start:%Y%m%d_%H%M%S}_{secrets.token_hex(2)}"
    return os.path.join(setup.frames_dir, session_id)


def _format_time_label(t: dt.datetime) -> str:
    return t.strftime("%Hh%M")


MIN_DERIVED_INTERVAL = 0.05  # floor for length-paced intervals (seconds)


def _session_capture_setup(setup: Setup, session_start: dt.datetime,
                           window_end: Optional[dt.datetime], log) -> Setup:
    """The Setup to capture this session with.

    Length-paced recordings (target_video_seconds set) get their interval
    derived fresh from this session's actual window duration, so the
    finished video comes out the target length even as daylight windows
    drift with the seasons. Interval-paced recordings pass through as-is,
    as does length-paced "always" mode (no window to divide by -- the GUI/
    CLI block that combination, but a hand-edited config shouldn't crash).
    """
    if not setup.target_video_seconds or window_end is None:
        return setup
    duration = (window_end - session_start).total_seconds()
    frames_needed = setup.target_video_seconds * setup.output_fps
    interval = max(duration / frames_needed, MIN_DERIVED_INTERVAL)
    log(f"Pacing for a ~{setup.target_video_seconds}s video at {setup.output_fps:g} fps: "
        f"1 frame every {interval:.2f}s over this {duration / 3600:.2f}h window.")
    return replace(setup, interval=interval)


def _auto_build_session(
    setup: Setup, session_dir: str, start: dt.datetime, end: dt.datetime, log
) -> None:
    if not any(Path(session_dir).glob("*.jpg")):
        log(f"No frames captured for '{setup.name}' this session -- skipping video build.")
        return
    label = f"{_format_time_label(start)}-{_format_time_label(end)}"
    filename = f"{setup.name}_{start:%Y-%m-%d}_{label}.mp4"
    output_file = os.path.join(setup.output_dir, filename)
    log(f"Building video for this session: {filename}")
    try:
        out = build_timelapse(setup, output_fps=setup.output_fps,
                              frames_dir=session_dir, output_file=output_file)
        log(f"Video ready: {out}")
    except (SystemExit, Exception) as e:
        log(f"Auto-build failed for '{setup.name}': {e}")


_MODE_LABELS = {
    "daylight": "daylight hours",
    "fixed_time": "fixed daily time window",
    "duration": "timer",
    "always": "continuous",
}


def run_scheduled(setup: Setup, log=print, stop_event: Optional[threading.Event] = None) -> None:
    stop_event = stop_event or threading.Event()
    mode = setup.schedule.mode
    label = _MODE_LABELS.get(mode, mode)
    if mode == "duration":
        label = f"{label}, {setup.schedule.duration_minutes} min"
    log(f"Starting scheduled capture for '{setup.name}' ({label}).")
    log("Press Ctrl+C to stop.")

    proc = None
    session_dir = None
    session_start = None
    try:
        while not stop_event.is_set():
            if setup.schedule.mode in ("daylight", "fixed_time"):
                window = next_window(setup)
                if _sleep_until(window.start, setup, log, stop_event):
                    break
                window_end = window.end
            elif setup.schedule.mode == "duration":
                window_end = _now(setup) + dt.timedelta(minutes=setup.schedule.duration_minutes)
            else:  # "always"
                window_end = None  # run forever

            session_start = _now(setup)
            session_dir = _new_session_dir(setup, session_start)
            log(f"Capturing for '{setup.name}' into session '{os.path.basename(session_dir)}'"
                + (f" until {window_end.strftime('%H:%M:%S %Z')}" if window_end else "")
                + "...")
            capture_setup = _session_capture_setup(setup, session_start, window_end, log)
            proc = start_capture_process(capture_setup, session_dir)

            while True:
                outcome = _wait_capture(proc, window_end, setup, stop_event)

                if outcome == "exited":
                    stderr = read_capture_stderr(proc)
                    log(f"ffmpeg exited unexpectedly (code {proc.returncode}). "
                        f"Retrying in {RETRY_DELAY_SECONDS}s. Last output:\n{stderr[-500:]}")
                    if stop_event.wait(RETRY_DELAY_SECONDS):
                        break
                    if window_end is not None and _now(setup) >= window_end:
                        break
                    proc = start_capture_process(capture_setup, session_dir)  # same session, retrying
                    continue
                break  # "window_end" or "stopped"

            log(f"Stopping capture for '{setup.name}' for now.")
            stop_capture_process(proc)
            proc = None
            session_end = _now(setup)
            _auto_build_session(setup, session_dir, session_start, session_end, log)
            session_dir = None
            session_start = None

            if setup.schedule.mode not in ("daylight", "fixed_time") or stop_event.is_set():
                break
    except KeyboardInterrupt:
        log("\nStopped by user.")
        if proc is not None:
            stop_capture_process(proc)
        if session_dir is not None:
            _auto_build_session(setup, session_dir, session_start, _now(setup), log)

    log(f"Scheduler for '{setup.name}' stopped.")
