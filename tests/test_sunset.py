"""Pure-logic tests for the daily sunset job: window math, segment
selection, staging, pruning. Nothing here needs ffmpeg or a camera."""

import datetime as dt
import threading

import pytest

from reolink_timelapse.config import SunsetJob
from reolink_timelapse import sunset as S

JOB = SunsetJob(camera_name="Backyard", latitude=36.088749, longitude=-83.819199,
                timezone="America/New_York")


def _naive(y, mo, d, h, mi, s=0):
    return dt.datetime(y, mo, d, h, mi, s)


def _local(y, mo, d, h, mi, s=0):
    """An aware time whose system-local clock reads the given values --
    what a segment named for that moment would carry."""
    return _naive(y, mo, d, h, mi, s).astimezone()


# --- window -----------------------------------------------------------

def test_window_is_sunset_plus_minus_offsets():
    w = S.sunset_window(JOB, dt.date(2026, 9, 10))
    assert w.date == dt.date(2026, 9, 10)
    assert w.sunset.date() == w.date
    assert w.sunset.tzname() == "EDT"
    expected = w.sunset.replace(hour=19, minute=49, second=0, microsecond=0)
    assert abs((w.sunset - expected).total_seconds()) < 180
    assert w.start == w.sunset - dt.timedelta(minutes=60)
    assert w.end == w.sunset + dt.timedelta(minutes=60)
    assert w.end - w.start == dt.timedelta(minutes=120)


def test_custom_offsets():
    job = SunsetJob(camera_name="x", latitude=JOB.latitude, longitude=JOB.longitude,
                    timezone=JOB.timezone, pre_minutes=30, post_minutes=90)
    w = S.sunset_window(job, dt.date(2026, 6, 21))
    assert w.end - w.start == dt.timedelta(minutes=120)
    assert w.sunset - w.start == dt.timedelta(minutes=30)


def test_dst_fall_back_changes_offset_not_correctness():
    before = S.sunset_window(JOB, dt.date(2026, 10, 31))
    after = S.sunset_window(JOB, dt.date(2026, 11, 1))
    assert before.sunset.utcoffset() == dt.timedelta(hours=-4)
    assert after.sunset.utcoffset() == dt.timedelta(hours=-5)
    assert before.sunset.hour == 18 and after.sunset.hour == 17  # clock jumps an hour
    # ...but real elapsed time between the two sunsets is still about a day.
    # (Compared via UTC: same-ZoneInfo aware datetimes subtract as wall clock.)
    elapsed = after.sunset.astimezone(dt.timezone.utc) - before.sunset.astimezone(dt.timezone.utc)
    assert abs(elapsed - dt.timedelta(days=1)) < dt.timedelta(minutes=5)


def test_unconfigured_job_is_a_clear_error():
    with pytest.raises(ValueError, match="sunset-config"):
        S.sunset_window(SunsetJob(camera_name="Backyard"), dt.date(2026, 9, 10))


# --- names ------------------------------------------------------------

def test_output_name_round_trip():
    d = dt.date(2026, 9, 10)
    assert S.output_name(d) == "Sunset_2026-09-10.mp4"
    assert S.output_date(S.output_name(d)) == d
    assert S.output_date("Sunset_2026-09-10.new.mp4") is None  # refresh_output's temp
    assert S.output_date("Backyard_2026-09-10_18h49-20h49.mp4") is None
    assert S.output_date("Sunset_.mp4") is None


# --- segment selection --------------------------------------------------

START = _naive(2026, 9, 10, 18, 49)
END = _naive(2026, 9, 10, 20, 49)


@pytest.mark.parametrize("name,inside", [
    ("20260910_184400_tl.mp4", False),  # ends exactly at start: excluded
    ("20260910_184401_tl.mp4", True),   # one second of overlap
    ("20260910_184900_tl.mp4", True),
    ("20260910_194500_tl.mp4", True),
    ("20260910_204859_tl.mp4", True),   # starts one second before end
    ("20260910_204900_tl.mp4", False),  # starts exactly at end: excluded
    ("20260910_210000_tl.mp4", False),
    ("garbage.mp4", False),
    ("2026091_tl.mp4", False),
])
def test_segment_overlaps(name, inside):
    assert S.segment_overlaps(name, START, END) is inside


def test_select_segments_sorted_and_filtered():
    names = ["20260910_195500_tl.mp4", "20260910_184400_tl.mp4", "junk.mp4",
             "20260910_190000_tl.mp4", "20260910_205000_tl.mp4"]
    assert S.select_segments(names, START, END) == [
        "20260910_190000_tl.mp4", "20260910_195500_tl.mp4"]


def test_later_segment_exists():
    assert not S.later_segment_exists(["20260910_204859_tl.mp4", "x.mp4"], END)
    assert S.later_segment_exists(["20260910_204900_tl.mp4"], END)
    assert S.later_segment_exists(["20260910_190000_tl.mp4", "20260911_010000_tl.mp4"], END)


def test_to_segment_clock_matches_how_segments_are_named():
    assert S.to_segment_clock(_local(2026, 9, 10, 18, 49)) == _naive(2026, 9, 10, 18, 49)


# --- staging ------------------------------------------------------------

def _window():
    return S.SunsetWindow(date=dt.date(2026, 9, 10), sunset=_local(2026, 9, 10, 19, 49),
                          start=_local(2026, 9, 10, 18, 49), end=_local(2026, 9, 10, 20, 49))


def _make_segments(segments_dir, names):
    segments_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (segments_dir / n).write_bytes(n.encode())


def test_stage_segments_links_only_in_window_and_is_idempotent(tmp_path):
    segments = tmp_path / "segments"
    staging = tmp_path / "staging"
    _make_segments(segments, ["20260910_180000_tl.mp4", "20260910_184500_tl.mp4",
                              "20260910_200000_tl.mp4", "20260910_210000_tl.mp4"])
    logs = []
    assert S.stage_segments(segments, staging, _window(), logs.append) == 2
    assert sorted(p.name for p in staging.iterdir()) == [
        "20260910_184500_tl.mp4", "20260910_200000_tl.mp4"]
    assert S.stage_segments(segments, staging, _window(), logs.append) == 0
    # Live deleting its copy must not touch ours (hardlink or fallback copy).
    (segments / "20260910_184500_tl.mp4").unlink()
    assert (staging / "20260910_184500_tl.mp4").read_bytes() == b"20260910_184500_tl.mp4"


def test_stage_segments_tolerates_missing_segments_dir(tmp_path):
    assert S.stage_segments(tmp_path / "nope", tmp_path / "staging", _window(), print) == 0


def test_collect_exits_when_a_later_segment_proves_window_is_final(tmp_path):
    segments = tmp_path / "segments"
    staging = tmp_path / "staging"
    w = _window()
    _make_segments(segments, ["20260910_190000_tl.mp4", "20260910_204500_tl.mp4",
                              "20260910_205000_tl.mp4"])  # last one starts after end
    stop = threading.Event()
    logs = []
    S.collect(w, segments, staging, stop, logs.append,
              now_fn=lambda: w.end + dt.timedelta(seconds=1))
    assert sorted(p.name for p in staging.iterdir()) == [
        "20260910_190000_tl.mp4", "20260910_204500_tl.mp4"]
    assert any("last segment is in" in m for m in logs)


def test_collect_stops_promptly_on_stop_event(tmp_path):
    w = _window()
    stop = threading.Event()
    stop.set()
    S.collect(w, tmp_path / "segments", tmp_path / "staging", stop, print,
              now_fn=lambda: w.start + dt.timedelta(minutes=10))  # mid-window


def test_finalize_with_nothing_staged_makes_no_video(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    logs = []
    assert S.finalize(_window(), staging, tmp_path / "out", logs.append) is None
    assert not staging.exists()
    assert not (tmp_path / "out" / "Sunset_2026-09-10.mp4").exists()
    assert any("live timelapse" in m for m in logs)


# --- pruning ------------------------------------------------------------

def test_prune_outputs_by_filename_date(tmp_path):
    for n in ["Sunset_2026-09-01.mp4", "Sunset_2026-09-02.mp4", "Sunset_2026-09-03.mp4",
              "Sunset_2026-09-09.mp4", "Sunset_2026-09-01.new.mp4", "other.mp4"]:
        (tmp_path / n).write_bytes(b"x")
    removed = S.prune_outputs(tmp_path, 7, dt.date(2026, 9, 10), print)
    assert removed == 2  # 09-01 and 09-02 are older than the 09-03 cutoff
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "Sunset_2026-09-01.new.mp4", "Sunset_2026-09-03.mp4", "Sunset_2026-09-09.mp4",
        "other.mp4"]


def test_prune_outputs_keep_forever(tmp_path):
    (tmp_path / "Sunset_2020-01-01.mp4").write_bytes(b"x")
    assert S.prune_outputs(tmp_path, None, dt.date(2026, 9, 10), print) == 0
    assert S.prune_outputs(tmp_path, 0, dt.date(2026, 9, 10), print) == 0
    assert (tmp_path / "Sunset_2020-01-01.mp4").exists()


# --- schedule listing ---------------------------------------------------

def test_print_schedule_csv(capsys):
    S.print_schedule(JOB, 3, start=dt.date(2026, 9, 10))
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "date,sunset,window_start,window_end"
    assert len(lines) == 4
    assert lines[1].startswith("2026-09-10,2026-09-10T19:4")
    assert lines[3].startswith("2026-09-12,")
