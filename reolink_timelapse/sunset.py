"""Daily sunset video, assembled from a camera's live rolling timelapse.

One video per day, `Sunset_<date>.mp4`, covering sunset minus `pre_minutes`
to sunset plus `post_minutes` (an hour either side by default). Sunset is
computed fresh each day for the job's location, so the window drifts
correctly with the seasons and the year rolls over without any table to
regenerate.

The footage is NOT captured here. The live view (live.py) already renders
the camera into 5-minute segments at 60x, and the same camera can't be
streamed twice without risking dropped frames in both (see the README's
note on camera load). So this job simply *borrows* the live segments: while
the window is open it polls the camera's segments folder and hardlinks
each in-window segment into a staging folder, and once the window closes
it concat-remuxes them (-c copy, no re-encode) into the day's video. Two
real hours at 60x come out as a ~2-minute 1080p video, identical in quality
to the live view, for no extra camera or CPU load.

Why hardlinks, and why during the window rather than after: live.py deletes
every segment but the newest twelve when its ~6-hour block rotates, and a
rotation can land inside the window. A hardlink is a second name for the
same file, so live's os.remove() of the original leaves the staged copy
intact at zero disk cost.

Knowing when the last segment has arrived: live converts chunks strictly
in order, so once a segment whose start time is at or after the window end
exists, every in-window segment that will ever exist is already on disk.
That's the normal exit; a hard deadline covers the case where live is down
and no later segment ever appears.

Segment filenames carry the *system* local clock (ffmpeg's -strftime), so
window edges are converted to that clock before comparing -- correct even
if the job's timezone differs from the machine's, though they should match.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import shutil
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, TextIO
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun

from .chunks import CHUNK_SECONDS, refresh_output
from .config import SunsetJob, app_root_dir
from .live import _segment_time, live_dirs
from .scheduler import sleep_until

SUNSET_POLL_SECONDS = 20
# Hard cap on waiting for the last in-window chunk after the window ends.
# Normally we leave early on the first *later* segment (see module doc);
# this only fires when live is down. The last in-window chunk can close up
# to one chunk after the window ends and, on a Pi 5 decoding in software
# at ~0.9x realtime, take about another chunk to convert -- so two chunks
# plus a margin.
SUNSET_GRACE_SECONDS = 2 * CHUNK_SECONDS + 300
OUTPUT_PREFIX = "Sunset_"
OUTPUT_GLOB = OUTPUT_PREFIX + "*.mp4"
SEGMENT_GLOB = "*_tl.mp4"

Log = Callable[[str], None]


@dataclass
class SunsetWindow:
    date: dt.date          # the sunset's local date; names the output file
    sunset: dt.datetime    # aware
    start: dt.datetime     # aware: sunset - pre_minutes
    end: dt.datetime       # aware: sunset + post_minutes


def sunset_dirs(camera_name: str) -> tuple[Path, Path]:
    """(output dir, staging root) for a camera's sunset videos, under the
    program folder like all other storage: Timelapses/Sunset/<camera>/."""
    root = app_root_dir() / "Timelapses" / "Sunset" / camera_name
    return root, root / ".staging"


def sunset_window(job: SunsetJob, date: dt.date) -> SunsetWindow:
    """Today's sunset at the job's location, widened by its pre/post minutes."""
    if not job.is_configured:
        raise ValueError(
            f"The sunset job for camera '{job.camera_name}' has no location -- run: "
            f"reolink-timelapse sunset-config --camera {job.camera_name} "
            f"--lat <latitude> --lon <longitude>")
    tz = ZoneInfo(job.timezone)
    loc = LocationInfo(latitude=job.latitude, longitude=job.longitude, timezone=job.timezone)
    s = sun(loc.observer, date=date, tzinfo=tz)
    sunset = s["sunset"]
    return SunsetWindow(
        date=date, sunset=sunset,
        start=sunset - dt.timedelta(minutes=job.pre_minutes),
        end=sunset + dt.timedelta(minutes=job.post_minutes),
    )


def output_name(date: dt.date) -> str:
    return f"{OUTPUT_PREFIX}{date:%Y-%m-%d}.mp4"


def output_date(name: str) -> Optional[dt.date]:
    """The date a finished video's name encodes, or None if the name isn't
    one of ours (including refresh_output's `*.new.mp4` temp file)."""
    if not (name.startswith(OUTPUT_PREFIX) and name.endswith(".mp4")):
        return None
    try:
        return dt.date.fromisoformat(name[len(OUTPUT_PREFIX):-len(".mp4")])
    except ValueError:
        return None


def to_segment_clock(t: dt.datetime) -> dt.datetime:
    """An aware time as the naive system-local clock reading ffmpeg used to
    name the segment (its -strftime runs on the machine's local time)."""
    return t.astimezone().replace(tzinfo=None)


def segment_overlaps(name: str, start: dt.datetime, end: dt.datetime,
                     chunk_seconds: int = CHUNK_SECONDS) -> bool:
    """Whether a segment named for its chunk's start covers any of
    [start, end). `start`/`end` are naive segment-clock times (see
    to_segment_clock). Names that don't parse never match."""
    s = _segment_time(Path(name))
    if s is None:
        return False
    return s < end and s + dt.timedelta(seconds=chunk_seconds) > start


def select_segments(names: Iterable[str], start: dt.datetime, end: dt.datetime) -> List[str]:
    return sorted(n for n in names if segment_overlaps(n, start, end))


def later_segment_exists(names: Iterable[str], end: dt.datetime) -> bool:
    """A segment starting at/after `end` means live has moved past the
    window: everything inside it is final (chunks convert in order)."""
    for n in names:
        s = _segment_time(Path(n))
        if s is not None and s >= end:
            return True
    return False


def _link_or_copy(src: Path, dst: Path) -> str:
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        # Cross-device, or a filesystem without hard links: a real copy
        # still survives live's pruning, it just costs the disk space.
        shutil.copy2(src, dst)
        return "copy"


def stage_segments(segments_dir: Path, staging_dir: Path, window: SunsetWindow,
                   log: Log) -> int:
    """Hardlink every in-window segment not already staged. Idempotent, so
    it doubles as the catch-up when starting mid-window. Returns how many
    were newly staged."""
    os.makedirs(staging_dir, exist_ok=True)
    start, end = to_segment_clock(window.start), to_segment_clock(window.end)
    names = (p.name for p in segments_dir.glob(SEGMENT_GLOB)) if segments_dir.is_dir() else ()
    new = 0
    for name in select_segments(names, start, end):
        dst = staging_dir / name
        if dst.exists():
            continue
        try:
            how = _link_or_copy(segments_dir / name, dst)
        except OSError as e:
            log(f"Sunset: couldn't stage {name}: {e}")
            continue
        new += 1
        if how == "copy":
            log(f"Sunset: {name} had to be copied rather than hardlinked "
                f"(different filesystem?) -- fine, just uses disk space.")
    return new


def collect(window: SunsetWindow, segments_dir: Path, staging_dir: Path,
            stop_event: threading.Event, log: Log,
            now_fn: Callable[[], dt.datetime]) -> None:
    """Stage segments as they appear until the window has closed and its
    last segment is in (or the grace deadline passes). Returns early,
    leaving the staging folder in place, if stopped."""
    deadline = window.end + dt.timedelta(seconds=SUNSET_GRACE_SECONDS)
    end_clock = to_segment_clock(window.end)
    total = 0
    while True:
        new = stage_segments(segments_dir, staging_dir, window, log)
        if new:
            total += new
            log(f"Sunset: staged {new} segment(s) for {window.date} ({total} this run).")
        now = now_fn()
        if now >= window.end:
            names = [p.name for p in segments_dir.glob(SEGMENT_GLOB)] if segments_dir.is_dir() else []
            if later_segment_exists(names, end_clock):
                log(f"Sunset: window for {window.date} closed and its last segment is in.")
                return
            if now >= deadline:
                log(f"Sunset: window for {window.date} closed {SUNSET_GRACE_SECONDS // 60} min "
                    f"ago with no newer segment from live -- finishing with what's staged.")
                return
        if stop_event.wait(SUNSET_POLL_SECONDS):
            return


def finalize(window: SunsetWindow, staging_dir: Path, out_dir: Path, log: Log) -> Optional[Path]:
    """Concat the staged segments into the day's video and clear staging.
    No segments (live wasn't running) -> no video, logged, None."""
    staged = sorted(staging_dir.glob(SEGMENT_GLOB)) if staging_dir.is_dir() else []
    if not staged:
        log(f"Sunset: no segments for {window.date} -- is the live timelapse for this "
            f"camera running? No video made.")
        shutil.rmtree(staging_dir, ignore_errors=True)
        return None
    os.makedirs(out_dir, exist_ok=True)
    out = out_dir / output_name(window.date)
    refresh_output(staged, out)  # lossless concat, written atomically
    shutil.rmtree(staging_dir, ignore_errors=True)
    expected = math.ceil((window.end - window.start).total_seconds() / CHUNK_SECONDS)
    try:
        size_mb = out.stat().st_size / 1e6
    except OSError:
        size_mb = 0
    log(f"Sunset: {out.name} ready -- sunset {window.sunset:%H:%M %Z}, window "
        f"{window.start:%H:%M}-{window.end:%H:%M}, {len(staged)} segment(s) "
        f"(~{expected} expected; {staged[0].name[:15]}..{staged[-1].name[:15]}), "
        f"{size_mb:.0f} MB.")
    return out


def prune_outputs(out_dir: Path, keep_days: Optional[int], today: dt.date, log: Log) -> int:
    """Delete finished videos dated more than keep_days before `today`,
    judged by the date in the filename. None/0 keeps everything."""
    if not keep_days or not out_dir.is_dir():
        return 0
    cutoff = today - dt.timedelta(days=keep_days)
    removed = 0
    for video in out_dir.glob(OUTPUT_GLOB):
        d = output_date(video.name)
        if d is None or d >= cutoff:
            continue
        try:
            os.remove(video)
            removed += 1
        except OSError as e:
            log(f"Sunset: couldn't prune {video.name}: {e}")
    if removed:
        log(f"Sunset: pruned {removed} video(s) older than {keep_days} day(s).")
    return removed


def build_once(job: SunsetJob, date: dt.date, log: Log = print) -> Optional[Path]:
    """Build one day's video right now from whatever in-window segments are
    on disk -- for trying the job out without waiting for tonight. Uses its
    own staging folder so it can't collide with a running daemon."""
    _, segments_dir, _ = live_dirs(job.camera_name)
    out_dir, staging_root = sunset_dirs(job.camera_name)
    window = sunset_window(job, date)
    staging = staging_root / f"{date:%Y-%m-%d}-once"
    shutil.rmtree(staging, ignore_errors=True)
    n = stage_segments(segments_dir, staging, window, log)
    log(f"Sunset: {n} segment(s) in {segments_dir} overlap {date}'s window "
        f"{window.start:%H:%M}-{window.end:%H:%M} {window.end:%Z}.")
    return finalize(window, staging, out_dir, log)


def print_schedule(job: SunsetJob, days: int, start: Optional[dt.date] = None,
                   out: Optional[TextIO] = None) -> None:
    """CSV of the next `days` sunsets and recording windows, ISO 8601 with
    UTC offsets -- a sanity check for the location, not a schedule file."""
    out = out or sys.stdout  # resolved at call time so captured stdout works
    date = start or dt.datetime.now(ZoneInfo(job.timezone)).date()
    out.write("date,sunset,window_start,window_end\n")
    for i in range(days):
        w = sunset_window(job, date + dt.timedelta(days=i))
        out.write(f"{w.date},{w.sunset.isoformat(timespec='seconds')},"
                  f"{w.start.isoformat(timespec='seconds')},"
                  f"{w.end.isoformat(timespec='seconds')}\n")


def _finish_leftovers(job: SunsetJob, staging_root: Path, out_dir: Path,
                      now: dt.datetime, log: Log) -> None:
    """Staging folders left by a run that died before finalising. Anything
    whose window (plus grace) is over gets built from what was staged;
    a folder for a day that already has a video is just cleared."""
    if not staging_root.is_dir():
        return
    grace = dt.timedelta(seconds=SUNSET_GRACE_SECONDS)
    for folder in sorted(p for p in staging_root.iterdir() if p.is_dir()):
        try:
            date = dt.date.fromisoformat(folder.name)
        except ValueError:
            continue  # "-once" folders and anything else that isn't ours
        window = sunset_window(job, date)
        if now < window.end + grace:
            continue  # today's, still in progress -- the daily loop resumes it
        if (out_dir / output_name(date)).exists():
            shutil.rmtree(folder, ignore_errors=True)
            continue
        log(f"Sunset: finishing {date} from segments staged by an earlier run.")
        try:
            finalize(window, folder, out_dir, log)
        except Exception as e:
            log(f"Sunset: couldn't finish leftover {date}: {e}")


def run_sunset(job: SunsetJob, stop_event: threading.Event, log: Log = print) -> None:
    """Daily loop; runs until stop_event is set. Never lets one bad day
    stop the next: any per-day failure is logged and the loop moves on."""
    camera = job.camera_name
    window = sunset_window(job, dt.date.today())  # validates the job up front
    _, segments_dir, _ = live_dirs(camera)
    out_dir, staging_root = sunset_dirs(camera)
    os.makedirs(out_dir, exist_ok=True)
    tz = ZoneInfo(job.timezone)

    def now_fn() -> dt.datetime:
        return dt.datetime.now(tz)

    keep = f"{job.keep_days} day(s)" if job.keep_days else "forever"
    log(f"Sunset: daily sunset video for '{camera}' from its live segments in "
        f"{segments_dir} -- {job.pre_minutes} min before to {job.post_minutes} min after "
        f"sunset at {job.latitude:.4f}, {job.longitude:.4f} ({job.timezone}); "
        f"videos go to {out_dir}, kept {keep}.")

    grace = dt.timedelta(seconds=SUNSET_GRACE_SECONDS)
    _finish_leftovers(job, staging_root, out_dir, now_fn(), log)
    date = now_fn().date()
    window = sunset_window(job, date)
    if now_fn() >= window.end + grace:
        date += dt.timedelta(days=1)  # today's is over (and was handled above if staged)

    while not stop_event.is_set():
        try:
            window = sunset_window(job, date)
            log(f"Sunset: {date} -- sunset {window.sunset:%H:%M %Z}, recording "
                f"{window.start:%H:%M}-{window.end:%H:%M}.")
            if sleep_until(window.start, now_fn, log, stop_event):
                break
            staging = staging_root / f"{date:%Y-%m-%d}"
            collect(window, segments_dir, staging, stop_event, log, now_fn)
            if stop_event.is_set():
                break  # staging stays on disk; the next start resumes from it
            finalize(window, staging, out_dir, log)
            prune_outputs(out_dir, job.keep_days, date, log)
        except Exception:
            log(f"Sunset: {date} failed:\n{traceback.format_exc()}")
            if stop_event.wait(60):
                break
        date += dt.timedelta(days=1)  # always advance: a bad day can't loop
    log(f"Sunset: stopped (camera '{camera}').")
