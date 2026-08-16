"""Long-running daily scheduler: captures only during the configured window.

For schedule.mode == "daylight" the window is today's actual sunrise/sunset
at the setup's location (recomputed every day, so it drifts correctly with
the seasons), widened by pre/post offset minutes. For "always" there is no
window and capture just runs continuously.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import time
from dataclasses import dataclass

from astral import LocationInfo
from astral.sun import sun
from zoneinfo import ZoneInfo

from .capture import start_capture_process, stop_capture_process
from .config import Setup

RETRY_DELAY_SECONDS = 30


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


def _sleep_until(target: dt.datetime, setup: Setup, log) -> None:
    remaining = (target - _now(setup)).total_seconds()
    if remaining > 0:
        log(f"Sleeping until {target.strftime('%Y-%m-%d %H:%M:%S %Z')} "
            f"({remaining / 3600:.1f}h)...")
        time.sleep(remaining)


def _next_window(setup: Setup) -> Window:
    now = _now(setup)
    window = daylight_window(setup, now.date())
    if now >= window.end:
        window = daylight_window(setup, now.date() + dt.timedelta(days=1))
    return window


def run_scheduled(setup: Setup, log=print) -> None:
    log(f"Starting scheduled capture for '{setup.name}' "
        f"({'daylight hours' if setup.schedule.mode == 'daylight' else 'continuous'}).")
    log("Press Ctrl+C to stop.")

    proc = None
    try:
        while True:
            if setup.schedule.mode == "daylight":
                window = _next_window(setup)
                _sleep_until(window.start, setup, log)
                window_end = window.end
            else:
                window_end = None  # run forever

            log(f"Capturing for '{setup.name}'"
                + (f" until {window_end.strftime('%H:%M:%S %Z')}" if window_end else "")
                + "...")
            proc = start_capture_process(setup)

            while True:
                if window_end is not None:
                    remaining = (window_end - _now(setup)).total_seconds()
                    if remaining <= 0:
                        break
                    exited = _wait_or_timeout(proc, remaining)
                else:
                    exited = _wait_or_timeout(proc, None)

                if exited:
                    stderr = proc.stderr.read() if proc.stderr else ""
                    log(f"ffmpeg exited unexpectedly (code {proc.returncode}). "
                        f"Retrying in {RETRY_DELAY_SECONDS}s. Last output:\n{stderr[-500:]}")
                    time.sleep(RETRY_DELAY_SECONDS)
                    if window_end is not None and _now(setup) >= window_end:
                        break
                    proc = start_capture_process(setup)
                    continue
                break  # window ended with ffmpeg still healthy

            log(f"Stopping capture for '{setup.name}' for now.")
            stop_capture_process(proc)
            proc = None

            if setup.schedule.mode != "daylight":
                break
    except KeyboardInterrupt:
        log("\nStopped by user.")
        if proc is not None:
            stop_capture_process(proc)


def _wait_or_timeout(proc, timeout: float | None) -> bool:
    """Returns True if the process exited within `timeout`, False if it's still running."""
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
