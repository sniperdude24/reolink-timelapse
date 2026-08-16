"""Long-running daily scheduler: captures only during the configured window.

For schedule.mode == "daylight" the window is today's actual sunrise/sunset
at the setup's location (recomputed every day, so it drifts correctly with
the seasons), widened by pre/post offset minutes. For "always" there is no
window and capture just runs continuously.

Every wait in here is chunked against a threading.Event rather than one long
blocking sleep, so a caller (the GUI, running this in a background thread)
can request a clean stop without waiting out an entire daylight window --
and so ffmpeg always gets terminated properly rather than orphaned.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

from astral import LocationInfo
from astral.sun import sun
from zoneinfo import ZoneInfo

from .capture import start_capture_process, stop_capture_process
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


def _now(setup: Setup) -> dt.datetime:
    if setup.schedule.timezone:
        return dt.datetime.now(ZoneInfo(setup.schedule.timezone))
    return dt.datetime.now().astimezone()


def _next_window(setup: Setup) -> Window:
    now = _now(setup)
    window = daylight_window(setup, now.date())
    if now >= window.end:
        window = daylight_window(setup, now.date() + dt.timedelta(days=1))
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


def run_scheduled(setup: Setup, log=print, stop_event: Optional[threading.Event] = None) -> None:
    stop_event = stop_event or threading.Event()
    log(f"Starting scheduled capture for '{setup.name}' "
        f"({'daylight hours' if setup.schedule.mode == 'daylight' else 'continuous'}).")
    log("Press Ctrl+C to stop.")

    proc = None
    try:
        while not stop_event.is_set():
            if setup.schedule.mode == "daylight":
                window = _next_window(setup)
                if _sleep_until(window.start, setup, log, stop_event):
                    break
                window_end = window.end
            else:
                window_end = None  # run forever

            log(f"Capturing for '{setup.name}'"
                + (f" until {window_end.strftime('%H:%M:%S %Z')}" if window_end else "")
                + "...")
            proc = start_capture_process(setup)

            while True:
                outcome = _wait_capture(proc, window_end, setup, stop_event)

                if outcome == "exited":
                    stderr = proc.stderr.read() if proc.stderr else ""
                    log(f"ffmpeg exited unexpectedly (code {proc.returncode}). "
                        f"Retrying in {RETRY_DELAY_SECONDS}s. Last output:\n{stderr[-500:]}")
                    if stop_event.wait(RETRY_DELAY_SECONDS):
                        break
                    if window_end is not None and _now(setup) >= window_end:
                        break
                    proc = start_capture_process(setup)
                    continue
                break  # "window_end" or "stopped"

            log(f"Stopping capture for '{setup.name}' for now.")
            stop_capture_process(proc)
            proc = None

            if setup.schedule.mode != "daylight" or stop_event.is_set():
                break
    except KeyboardInterrupt:
        log("\nStopped by user.")
        if proc is not None:
            stop_capture_process(proc)

    log(f"Scheduler for '{setup.name}' stopped.")
