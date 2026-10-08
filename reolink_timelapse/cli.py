from __future__ import annotations

import argparse
import datetime as dt
import getpass
import sys
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Camera, Config, Recording, Schedule, Setup, SunsetJob, is_valid_timezone
from .chunks import refresh_output
from .scheduler import daylight_window, run_scheduled
from .webstream import start_stream_server, stream_url


def _prompt(label: str, default=None, cast=str):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if not raw:
            if default is not None:
                return default
            print("  This value is required.")
            continue
        try:
            return cast(raw)
        except ValueError:
            print(f"  Please enter a valid {cast.__name__}.")


def _prompt_choice(label: str, options: list, default: str) -> str:
    opts_str = "/".join(options)
    while True:
        raw = _prompt(f"{label} ({opts_str})", default)
        if raw in options:
            return raw
        print(f"  Please enter one of: {opts_str}")


def _prompt_timezone(label: str, default=None) -> str:
    while True:
        tz_name = _prompt(label, default)
        if is_valid_timezone(tz_name):
            return tz_name
        print(f"  '{tz_name}' isn't a recognized IANA timezone name. "
              f"Use the Region/City form, e.g. America/New_York, Europe/London, "
              f"Asia/Tokyo (full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).")


def _prompt_yes_no(label: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{label} [{suffix}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def _open_config() -> Config:
    config = Config()
    if config.migrated_from:
        print(f"(Migrated existing config from {config.migrated_from} to {config.path}.)\n")
    if config.migration_error:
        print(f"WARNING: {config.migration_error}\n")
    return config


def _prompt_schedule(existing: Optional[Schedule]) -> Schedule:
    sched = existing or Schedule()
    mode = _prompt_choice(
        "Schedule mode", ["daylight", "fixed_time", "duration", "always"], sched.mode
    )
    if mode == "daylight":
        latitude = _prompt("Latitude (e.g. 40.4406)", sched.latitude, float)
        longitude = _prompt("Longitude (e.g. -79.9959)", sched.longitude, float)
        timezone = _prompt_timezone(
            "IANA timezone name (e.g. America/New_York)", sched.timezone
        )
        pre_offset = _prompt(
            "Start capturing this many minutes before sunrise", sched.pre_offset_minutes, int
        )
        post_offset = _prompt(
            "Keep capturing this many minutes after sunset", sched.post_offset_minutes, int
        )
        return Schedule(
            mode=mode, latitude=latitude, longitude=longitude, timezone=timezone,
            pre_offset_minutes=pre_offset, post_offset_minutes=post_offset,
        )
    if mode == "fixed_time":
        timezone = _prompt_timezone(
            "IANA timezone name (e.g. America/New_York)", sched.timezone
        )
        start_time = _prompt("Start time, 24h HH:MM", sched.start_time or "07:00")
        end_time = _prompt("End time, 24h HH:MM", sched.end_time or "19:00")
        return Schedule(mode=mode, timezone=timezone, start_time=start_time, end_time=end_time)
    if mode == "duration":
        duration = _prompt("Run for how many minutes", sched.duration_minutes or 60, int)
        return Schedule(mode=mode, duration_minutes=duration)
    return Schedule(mode="always")


def _session_seconds_today(sched: Schedule) -> Optional[float]:
    """Length of one capture session for today, or None when the schedule
    has no defined length (always mode / bad daylight fields). Best-effort;
    feeds the recommended-rate line only."""
    if sched.mode == "duration":
        return sched.duration_minutes * 60 if sched.duration_minutes else None
    if sched.mode == "fixed_time":
        delta = (dt.datetime.strptime(sched.end_time, "%H:%M")
                 - dt.datetime.strptime(sched.start_time, "%H:%M")).total_seconds()
        return delta if delta > 0 else delta + 86400  # overnight window
    if sched.mode == "daylight":
        try:
            temp = Setup(name="", ip="", user="", password="", schedule=sched)
            window = daylight_window(temp, dt.date.today())
            return (window.end - window.start).total_seconds()
        except Exception:
            return None
    return None


def cmd_configure(args: argparse.Namespace) -> None:
    config = _open_config()
    existing = config.cameras.get(args.name)

    print(f"Configuring camera '{args.name}'" + (" (editing existing)" if existing else "") + "\n")

    ip = _prompt("Camera IP address", existing.ip if existing else None)
    port = _prompt("RTSP port", existing.port if existing else 554, int)
    user = _prompt("Camera username", existing.user if existing else None)

    pw_prompt = "Camera password" + (" [leave blank to keep current]" if existing else "")
    password = getpass.getpass(f"{pw_prompt}: ")
    if not password:
        if existing:
            password = existing.password
        else:
            print("  A password is required.")
            password = getpass.getpass("Camera password: ")

    channel = _prompt("Camera channel number", existing.channel if existing else 1, int)
    substream = _prompt_yes_no(
        "Use the lower-res substream (recommended)?",
        existing.substream if existing else True,
    )

    crf = int(_prompt_choice(
        "Encode quality CRF (lower = better quality, larger files)",
        ["20", "23", "26", "28"],
        str(existing.crf if existing else 23),
    ))

    decode_mode = existing.decode_mode if existing else "software"
    from .decode import hw_decode_platform, hw_mechanism_name
    if hw_decode_platform():
        mechanism = hw_mechanism_name()
        want_hw = _prompt_yes_no(
            f"Try hardware video decode ({mechanism}) for this camera? "
            f"EXPERIMENTAL -- unvalidated on this camera's stream; run "
            f"'selftest-decode --camera {args.name}' before trusting it",
            decode_mode == "hardware",
        )
        decode_mode = "hardware" if want_hw else "software"

    camera = Camera(
        name=args.name, ip=ip, port=port, user=user, password=password,
        channel=channel, substream=substream, decode_mode=decode_mode, crf=crf,
    )
    config.put_camera(camera)
    config.save()
    print(f"\nSaved camera '{args.name}' to {config.path}")


def cmd_record(args: argparse.Namespace) -> None:
    config = _open_config()
    existing = config.recordings.get(args.name)

    camera_name = args.camera or (existing.camera_name if existing else None)
    if not camera_name:
        raise SystemExit("--camera is required when adding a new recording.")
    config.get_camera(camera_name)  # raises a clear error if it doesn't exist

    print(f"Configuring recording '{args.name}' (camera: {camera_name})"
          + (" (editing existing)" if existing else "") + "\n")

    while True:
        output_fps = _prompt("Output video fps", existing.output_fps if existing else 30, float)
        if output_fps > 0:
            break
        print("  fps must be greater than 0.")

    sched = _prompt_schedule(existing.schedule if existing else None)

    # Pacing: with a bounded schedule the interval can instead be derived
    # each session from a target video length; "always" has no session
    # length to divide by, so it stays interval-only.
    interval = existing.interval if existing else 30
    target_video_seconds = None
    pacing = "interval"
    if sched.mode != "always":
        default_pacing = "video_length" if (existing and existing.target_video_seconds) else "interval"
        pacing = _prompt_choice("Set capture by", ["interval", "video_length"], default_pacing)
    if pacing == "video_length":
        while True:
            target_video_seconds = _prompt(
                "Target video length in seconds",
                existing.target_video_seconds if (existing and existing.target_video_seconds) else 60,
                int,
            )
            if target_video_seconds > 0:
                break
            print("  Length must be greater than 0.")
        session_secs = _session_seconds_today(sched)
        if session_secs:
            rec = max(session_secs / (target_video_seconds * output_fps), 0.05)
            print(f"  Recommended capture rate: 1 frame every {rec:.2f}s for today's "
                  f"{session_secs / 3600:.1f}h window -- auto-adjusts each session.")
    else:
        while True:
            interval = _prompt("Seconds between frames", existing.interval if existing else 30, float)
            if interval > 0:
                break
            print("  Interval must be greater than 0.")

    recording = Recording(
        name=args.name, camera_name=camera_name, interval=interval,
        output_fps=output_fps, target_video_seconds=target_video_seconds, schedule=sched,
    )
    config.put_recording(recording)
    config.save()
    print(f"\nSaved recording '{args.name}' to {config.path}")
    print(f"Frames and videos will save under: {recording.output_dir}")


def cmd_list(args: argparse.Namespace) -> None:
    config = _open_config()
    if not config.cameras and not config.recordings:
        print("Nothing configured yet. Run 'reolink-timelapse configure <name>' to add a camera.")
        return

    print("Cameras:")
    if not config.cameras:
        print("  (none)")
    for name, c in sorted(config.cameras.items()):
        print(f"  {name}: {c.ip}:{c.port} channel {c.channel} "
              f"({'sub' if c.substream else 'main'} stream)")

    print("\nRecordings:")
    if not config.recordings:
        print("  (none)")
    for name, r in sorted(config.recordings.items()):
        print(f"  {name}: camera={r.camera_name} every {r.interval}s schedule={r.schedule.mode}")

    if config.sunset_jobs:
        print("\nSunset videos (daily, from the live timelapse):")
        for name, j in sorted(config.sunset_jobs.items()):
            print(f"  {name}: {j.pre_minutes} min before to {j.post_minutes} min after sunset "
                  f"at {j.latitude}, {j.longitude} ({j.timezone}); "
                  f"keep {f'{j.keep_days} days' if j.keep_days else 'forever'}")


def cmd_remove_camera(args: argparse.Namespace) -> None:
    config = _open_config()
    config.get_camera(args.name)  # raises a clear error if missing
    config.remove_camera(args.name)  # raises if a recording still references it
    config.save()
    print(f"Removed camera '{args.name}'.")


def cmd_remove_recording(args: argparse.Namespace) -> None:
    config = _open_config()
    config.get_recording(args.name)  # raises a clear error if missing
    config.remove_recording(args.name)
    config.save()
    print(f"Removed recording '{args.name}'.")


def cmd_run(args: argparse.Namespace) -> None:
    config = _open_config()
    setup = config.resolved(args.name)
    run_scheduled(setup)


def cmd_live(args: argparse.Namespace) -> None:
    import threading

    from .live import run_live

    config = _open_config()
    camera = config.get_camera(args.camera)
    # run_live is the one capture path that writes last_hour.mp4/session.mp4
    # under Timelapses/Live/<camera>/ -- the files webstream.py serves --
    # so this is the CLI path where starting it and printing the URL means
    # something. (Scheduled recordings via `run` have no such live view.)
    start_stream_server(host=config.stream_bind_host, log=print,
                        auth=config.stream_auth)
    print(f"Watch live at: {stream_url(camera.name)}")
    stop_event = threading.Event()
    worker = threading.Thread(
        target=run_live, args=(camera, stop_event),
        kwargs={"sessions_keep_days": config.live_sessions_keep_days}, daemon=True)
    worker.start()
    print("Live timelapse running -- press Ctrl+C to stop.")
    try:
        while worker.is_alive():
            worker.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nStopping live timelapse (finishing the current chunk)...")
        stop_event.set()
        worker.join()


def cmd_sunset_config(args: argparse.Namespace) -> None:
    """Add or edit a camera's daily sunset job. Flags rather than prompts:
    this is the one setup step a headless install does over SSH, and it
    must be scriptable. Editing an existing job only needs the flags
    that change."""
    import tzlocal

    from .sunset import sunset_dirs, sunset_window

    config = _open_config()
    config.get_camera(args.camera)  # a clear error if the camera doesn't exist
    existing = config.sunset_jobs.get(args.camera)

    def pick(flag, current, default):
        if flag is not None:
            return flag
        return current if existing else default

    lat = pick(args.lat, existing.latitude if existing else None, None)
    lon = pick(args.lon, existing.longitude if existing else None, None)
    if lat is None or lon is None:
        raise SystemExit("--lat and --lon are required when adding a new sunset job.")
    if not -90 <= lat <= 90:
        raise SystemExit("--lat must be between -90 and 90.")
    if not -180 <= lon <= 180:
        raise SystemExit("--lon must be between -180 and 180.")
    tz = pick(args.tz, existing.timezone if existing else None, None) or tzlocal.get_localzone_name()
    if not is_valid_timezone(tz):
        raise SystemExit(f"'{tz}' isn't a recognized IANA timezone name -- use the "
                         f"Region/City form, e.g. America/New_York.")
    pre = pick(args.pre, existing.pre_minutes if existing else None, 60)
    post = pick(args.post, existing.post_minutes if existing else None, 60)
    keep = pick(args.keep_days, existing.keep_days if existing else None, 7)
    if pre < 0 or post < 0:
        raise SystemExit("--pre and --post must be 0 or more minutes.")
    if keep is not None and keep < 0:
        raise SystemExit("--keep-days must be 0 (keep forever) or more.")

    job = SunsetJob(camera_name=args.camera, latitude=lat, longitude=lon, timezone=tz,
                    pre_minutes=pre, post_minutes=post, keep_days=keep or None)
    config.put_sunset(job)
    config.save()

    out_dir, _ = sunset_dirs(args.camera)
    print(f"Saved sunset job for camera '{args.camera}' to {config.path}")
    print(f"  Location {lat}, {lon} ({tz}); recording {pre} min before to {post} min "
          f"after sunset; videos kept {f'{keep} days' if keep else 'forever'} here.")
    today = dt.datetime.now(ZoneInfo(tz)).date()
    w = sunset_window(job, today)
    print(f"  Today ({today}): sunset {w.sunset:%H:%M %Z}, window {w.start:%H:%M}-{w.end:%H:%M}.")
    print(f"Videos will save as {out_dir / 'Sunset_<date>.mp4'}")
    print(f"Run it with: reolink-timelapse sunset --camera {args.camera}   "
          f"(needs 'live --camera {args.camera}' running -- it borrows that footage)")


def cmd_sunset(args: argparse.Namespace) -> None:
    import signal
    import threading

    from .sunset import build_once, print_schedule, run_sunset

    config = _open_config()
    job = config.get_sunset(args.camera)
    if not job.is_configured:
        raise SystemExit(f"The sunset job for '{args.camera}' has no location. Run: "
                         f"reolink-timelapse sunset-config --camera {args.camera} "
                         f"--lat <latitude> --lon <longitude>")
    if args.date and not args.once:
        raise SystemExit("--date only makes sense with --once.")

    if args.print_schedule:
        print_schedule(job, args.print_schedule)
        return
    if args.once:
        date = args.date or dt.datetime.now(ZoneInfo(job.timezone)).date()
        out = build_once(job, date, log=print)
        if out is None:
            sys.exit(1)
        print(f"Saved: {out}")
        return

    stop_event = threading.Event()
    # `systemctl stop` sends SIGTERM: turn it into a clean stop so a
    # half-collected window stays staged for the next start instead of
    # being killed mid-write.
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    except (ValueError, OSError, AttributeError):
        pass  # not the main thread, or a platform without SIGTERM
    worker = threading.Thread(target=run_sunset, args=(job, stop_event),
                              kwargs={"log": print}, daemon=True)
    worker.start()
    print("Sunset job running -- press Ctrl+C to stop.")
    try:
        while worker.is_alive():
            worker.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nStopping (any half-collected window is kept for next time)...")
        stop_event.set()
        worker.join()


def _rebuild_latest_session(setup) -> bool:
    """Rejoin the newest recorded session, if there is one.

    Recordings capture video and render clips as they go, so rebuilding one
    is a lossless concat -- its pacing was fixed at capture time.
    """
    root = Path(setup.output_dir) / "sessions"
    if not root.is_dir():
        return False
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        segments = sorted((d / "segments").glob("*_tl.mp4"))
        if not segments:
            continue
        out = Path(setup.output_dir) / f"{setup.name}_{d.name[:15]}_rebuild.mp4"
        print(f"Rejoining {len(segments)} clip(s) from session {d.name}...")
        refresh_output(segments, out)
        print(f"\nDone! Saved to: {out}")
        return True
    return False


def cmd_build(args: argparse.Namespace) -> None:
    config = _open_config()
    setup = config.resolved(args.name)
    if not _rebuild_latest_session(setup):
        sys.exit(f"ERROR: no recorded sessions with rendered clips found for "
                 f"'{args.name}' -- run it once first.")


def cmd_gui(args: argparse.Namespace) -> None:
    from .gui import main as gui_main
    gui_main()


def cmd_selftest_decode(args: argparse.Namespace) -> None:
    """A/B a camera's real stream, software decode vs hardware, the same
    way the Windows NVDEC rejection was originally established -- capture
    real footage, decode it both ways, and count how many frames actually
    differ, rather than assuming either is safe.
    """
    import os
    import re
    import subprocess
    import tempfile
    import time

    from .capture import stop_capture_process
    from .chunks import convert_chunk, start_chunk_capture
    from .decode import decoder_map_for_platform, hw_decoders_available, probe_codec
    from .rtsp import check_ffmpeg, no_console_kwargs

    ffmpeg_bin = check_ffmpeg()
    config = _open_config()
    camera = config.get_camera(args.camera)

    print(f"Probing '{args.camera}'...")
    codec = probe_codec(camera, ffmpeg_bin)
    if codec is None:
        sys.exit("ERROR: couldn't determine the camera's video codec "
                 "(is the stream reachable?).")
    print(f"Codec: {codec}")
    hw_decoder = decoder_map_for_platform().get(codec)
    if hw_decoder is None:
        sys.exit(f"ERROR: no known hardware decoder exists for '{codec}' on this platform.")
    if hw_decoder not in hw_decoders_available(ffmpeg_bin):
        sys.exit(f"ERROR: this ffmpeg build doesn't expose '{hw_decoder}' -- "
                 f"hardware decode isn't available on this system.")

    with tempfile.TemporaryDirectory(prefix="reolink_selftest_") as tmp_str:
        tmp = Path(tmp_str)
        chunks_dir, segments_dir = tmp / "chunks", tmp / "segments"
        chunks_dir.mkdir()
        print(f"Capturing {args.seconds}s of real stream from '{args.camera}'...")
        proc = start_chunk_capture(camera, chunks_dir, chunk_seconds=args.seconds + 30)
        time.sleep(args.seconds)
        stop_capture_process(proc)
        chunk_files = sorted(chunks_dir.glob("*.ts"))
        if not chunk_files:
            sys.exit("ERROR: no chunk was captured -- check the camera connection.")
        chunk = chunk_files[0]

        print("Decoding in software...")
        sw_path = segments_dir / "sw.mp4"
        sw_start = time.monotonic()
        os.replace(convert_chunk(chunk, segments_dir, interval=1.0, output_fps=10), sw_path)
        sw_secs = time.monotonic() - sw_start
        print(f"Decoding with '{hw_decoder}'...")
        hw_path = segments_dir / "hw.mp4"
        hw_start = time.monotonic()
        try:
            os.replace(convert_chunk(chunk, segments_dir, interval=1.0, output_fps=10,
                                     hw_decoder=hw_decoder), hw_path)
        except RuntimeError as e:
            # ffmpeg listing a decoder doesn't prove the hardware behind it
            # exists: a Pi 5 build still lists h264/hevc_v4l2m2m even
            # though the Pi 5 dropped those general decode blocks, so
            # opening the decoder fails at runtime ("Could not find a
            # valid device"). That IS the self-test's answer, not a crash.
            # (A Pi 5 does have an HEVC-only block, rpivid, which this
            # test exercises via the 'hwaccel:drm' spec instead.)
            sys.exit(
                f"\nVERDICT: '{hw_decoder}' exists in this ffmpeg build but "
                f"FAILED to run on this hardware:\n  {e}\n"
                f"This machine cannot hardware-decode {codec} this way. "
                f"Software decode -- the default -- is the correct setting; "
                f"leave decode_mode alone.")
        hw_secs = time.monotonic() - hw_start
        print(f"Decode time: software {sw_secs:.1f}s, hardware {hw_secs:.1f}s.")
        if hw_decoder.startswith("hwaccel:") and hw_secs > sw_secs * 0.8:
            # -hwaccel is advisory: when it can't engage, ffmpeg silently
            # decodes in software, which would make the A/B below compare
            # software against itself -- a meaningless PASS. Real hardware
            # decode is dramatically faster (measured 3.2x realtime vs
            # 0.9x on the Pi 5), so near-equal times mean it didn't engage.
            print("WARNING: the hardware leg was not clearly faster than "
                  "software -- the hwaccel may not have engaged, in which "
                  "case this comparison proves nothing. Treat a PASS below "
                  "with suspicion.")

        print("Comparing frame-by-frame (SSIM)...")
        stats_file = tmp / "ssim.txt"
        # ffmpeg's filter-graph parser treats ':' as an option separator,
        # so a Windows drive-letter path (C:\...) breaks -lavfi option
        # parsing even when escaped -- side-step it entirely by running
        # ffmpeg with the temp dir as cwd and passing a bare filename.
        cmd = [ffmpeg_bin, "-i", str(sw_path), "-i", str(hw_path),
               "-lavfi", f"ssim=stats_file={stats_file.name}", "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp, **no_console_kwargs())
        if not stats_file.exists():
            sys.exit(f"ERROR: comparison failed:\n{(r.stderr or '')[-500:]}")

        threshold = 0.98
        total = damaged = 0
        for line in stats_file.read_text(encoding="utf-8").splitlines():
            m = re.search(r"All:([\d.]+)", line)
            if m:
                total += 1
                if float(m.group(1)) < threshold:
                    damaged += 1

    print(f"\n{damaged} of {total} frames differ significantly between software and "
         f"'{hw_decoder}' decode (SSIM below {threshold}).")
    if damaged:
        print("Hardware decode looks unsafe for this camera's stream -- keep "
             "decode_mode set to 'software' for it.")
    else:
        print("No significant differences found on this clip. That's a good sign, "
             "not a guarantee -- this was one short capture, not a long real session. "
             "Watch a longer run before fully trusting hardware decode here.")


def cmd_users(args: argparse.Namespace) -> None:
    """Manage the stream server's login accounts from the command line --
    the same store the web UI's /users page edits, so this is how a
    headless or containerised install creates its first admin (inside
    Docker the host isn't loopback, so the "open localhost:8177/users on
    the server itself" bootstrap doesn't apply). A running server picks
    up changes immediately; no restart needed."""
    from .webusers import UserStore, MIN_PIN_LEN

    store = UserStore()
    if args.action == "list":
        users = store.list_users()
        if not users:
            print("No users -- the stream server is open (no login). "
                  "Add one with: reolink-timelapse users add <name> --admin")
        for u in users:
            print(f"  {u['name']}" + ("  (admin)" if u["admin"] else ""))
        return
    if not args.name:
        raise SystemExit(f"'users {args.action}' needs a username.")
    if args.action == "remove":
        try:
            store.remove(args.name)
        except ValueError as e:
            raise SystemExit(f"ERROR: {e}")
        print(f"Removed '{args.name}' (their logged-in devices stop working now).")
        return
    pin = args.pin
    if pin is None:
        pin = getpass.getpass(f"PIN for '{args.name}' ({MIN_PIN_LEN}+ characters): ")
        if pin != getpass.getpass("Repeat PIN: "):
            raise SystemExit("PINs didn't match.")
    try:
        store.put(args.name, pin, admin=args.admin)
    except ValueError as e:
        raise SystemExit(f"ERROR: {e}")
    print(f"Saved '{args.name}'" + (" as an admin" if args.admin else "")
          + f" in {store._path}")


def cmd_serve_stream(args: argparse.Namespace) -> None:
    """Run the live-view web server on its own, decoupled from any one
    camera's capture process.

    `live --camera X` already starts this server too, but its lifetime is
    then tied to that one camera process. On a headless box running
    several `live` processes as separate services, whichever started
    first happens to own the server -- the stream keeps working (it reads
    Timelapses/Live/ off disk, not from any specific process's memory),
    but that coupling is accidental. Run this as its own always-on
    service instead and every camera's live view stays reachable
    regardless of which capture processes are up.
    """
    import time

    config = _open_config()
    start_stream_server(host=config.stream_bind_host, log=print,
                        auth=config.stream_auth)
    print("Live stream server running -- press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="reolink-timelapse",
        description="Record and build timelapses from Reolink (or any RTSP) cameras, "
                     "across any number of camera sources and scheduled recordings.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("configure", help="Add or edit a camera source")
    p.add_argument("name", help="Short name for this camera, e.g. 'backyard'")
    p.set_defaults(func=cmd_configure)

    p = sub.add_parser("record", help="Add or edit a scheduled recording")
    p.add_argument("name", help="Short name for this recording")
    p.add_argument("--camera", default=None,
                    help="Name of an already-configured camera (required when adding new)")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("list", help="List configured cameras and recordings")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("remove-camera", help="Remove a configured camera")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove_camera)

    p = sub.add_parser("remove-recording", help="Remove a configured recording")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove_recording)

    p = sub.add_parser("run", help="Start capturing for a recording (long-running)")
    p.add_argument("name")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("live", help="Run a rolling near-live timelapse for a camera "
                                     "(updates last_hour.mp4 and session.mp4 every ~5 min)")
    p.add_argument("--camera", required=True, help="Name of a configured camera")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("sunset-config", help="Add or edit a camera's daily sunset video job "
                                              "(location + how far either side of sunset); "
                                              "flags, not prompts, so it works over SSH")
    p.add_argument("--camera", required=True, help="Name of a configured camera")
    p.add_argument("--lat", type=float, default=None, help="Latitude, decimal degrees")
    p.add_argument("--lon", type=float, default=None, help="Longitude, decimal degrees")
    p.add_argument("--tz", default=None,
                    help="IANA timezone, e.g. America/New_York (default: this machine's)")
    p.add_argument("--pre", type=int, default=None,
                    help="Minutes before sunset to start (default: 60)")
    p.add_argument("--post", type=int, default=None,
                    help="Minutes after sunset to stop (default: 60)")
    p.add_argument("--keep-days", type=int, default=None, dest="keep_days",
                    help="Delete finished videos older than this many days; 0 = keep "
                         "forever (default: 7)")
    p.set_defaults(func=cmd_sunset_config)

    p = sub.add_parser("sunset", help="Make one sunset video per day for a camera, assembled "
                                       "from its running live timelapse (long-running; "
                                       "configure it first with 'sunset-config')")
    p.add_argument("--camera", required=True, help="Name of a configured camera")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                       help="Build one day's video now from the segments currently on "
                            "disk, then exit -- for trying it out")
    mode.add_argument("--print-schedule", type=int, default=None, metavar="DAYS",
                       help="Print the next DAYS sunsets and recording windows as CSV, "
                            "then exit -- a sanity check for the location")
    p.add_argument("--date", type=dt.date.fromisoformat, default=None,
                    help="With --once: which day to build (YYYY-MM-DD; default: today)")
    p.set_defaults(func=cmd_sunset)

    p = sub.add_parser("gui", help="Launch the graphical control panel")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("selftest-decode", help="EXPERIMENTAL: A/B a camera's real stream, "
                                                "software vs hardware decode, and report how "
                                                "many frames differ -- run before trusting "
                                                "decode_mode='hardware' on any camera")
    p.add_argument("--camera", required=True, help="Name of a configured camera")
    p.add_argument("--seconds", type=int, default=20,
                    help="How many seconds of real stream to capture for the test (default: 20)")
    p.set_defaults(func=cmd_selftest_decode)

    p = sub.add_parser("serve-stream", help="Run the live-view web server on its own "
                                             "(long-running) -- lets 'Watch in VLC'-style "
                                             "URLs work independently of any one camera's "
                                             "capture process, e.g. as its own systemd unit")
    p.set_defaults(func=cmd_serve_stream)

    p = sub.add_parser("users", help="Manage stream-server login accounts: "
                                      "'users list', 'users add <name> [--admin] [--pin X]', "
                                      "'users remove <name>' -- the same accounts the web "
                                      "UI's /users page manages; a running server sees "
                                      "changes immediately")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("name", nargs="?", help="Username (for add/remove)")
    p.add_argument("--admin", action="store_true",
                    help="This user may manage other users (add)")
    p.add_argument("--pin", default=None,
                    help="PIN/passphrase (add); prompted for if omitted")
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("build", help="Rejoin the newest recorded session into an mp4 "
                                      "(lossless -- pacing was fixed at capture time)")
    p.add_argument("name")
    p.set_defaults(func=cmd_build)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
