from __future__ import annotations

import argparse
import datetime as dt
import getpass
import sys
from pathlib import Path
from typing import Optional

from .config import Camera, Config, Recording, Schedule, Setup, is_valid_timezone
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

    decode_mode = existing.decode_mode if existing else "software"
    from .decode import hw_decode_platform
    if hw_decode_platform():
        want_hw = _prompt_yes_no(
            "Try hardware video decode (EXPERIMENTAL -- unvalidated on this "
            "camera's stream; run 'selftest-decode' before trusting it)?",
            decode_mode == "hardware",
        )
        decode_mode = "hardware" if want_hw else "software"

    camera = Camera(
        name=args.name, ip=ip, port=port, user=user, password=password,
        channel=channel, substream=substream, decode_mode=decode_mode,
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
    start_stream_server(host=config.stream_bind_host, log=print)
    print(f"Watch live at: {stream_url(camera.name)}")
    stop_event = threading.Event()
    worker = threading.Thread(target=run_live, args=(camera, stop_event), daemon=True)
    worker.start()
    print("Live timelapse running -- press Ctrl+C to stop.")
    try:
        while worker.is_alive():
            worker.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nStopping live timelapse (finishing the current chunk)...")
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
    from .decode import HW_DECODERS, hw_decoders_available, probe_codec
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
    hw_decoder = HW_DECODERS.get(codec)
    if hw_decoder is None:
        sys.exit(f"ERROR: no known hardware decoder exists for '{codec}' on this project.")
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
        os.replace(convert_chunk(chunk, segments_dir, interval=1.0, output_fps=10), sw_path)
        print(f"Decoding with '{hw_decoder}'...")
        hw_path = segments_dir / "hw.mp4"
        os.replace(convert_chunk(chunk, segments_dir, interval=1.0, output_fps=10,
                                 hw_decoder=hw_decoder), hw_path)

        print("Comparing frame-by-frame (SSIM)...")
        stats_file = tmp / "ssim.txt"
        cmd = [ffmpeg_bin, "-i", str(sw_path), "-i", str(hw_path),
               "-lavfi", f"ssim=stats_file={stats_file}", "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, text=True, **no_console_kwargs())
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
    start_stream_server(host=config.stream_bind_host, log=print)
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

    p = sub.add_parser("build", help="Rejoin the newest recorded session into an mp4 "
                                      "(lossless -- pacing was fixed at capture time)")
    p.add_argument("name")
    p.set_defaults(func=cmd_build)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
