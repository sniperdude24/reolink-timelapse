from __future__ import annotations

import argparse
import datetime as dt
import getpass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config, Setup, Schedule
from .build import build_timelapse
from .scheduler import run_scheduled


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


def _prompt_timezone(label: str, default=None) -> str:
    while True:
        tz_name = _prompt(label, default)
        try:
            ZoneInfo(tz_name)
            return tz_name
        except ZoneInfoNotFoundError:
            print(f"  '{tz_name}' isn't a recognized IANA timezone name. "
                  f"Use the Region/City form, e.g. America/New_York, Europe/London, "
                  f"Asia/Tokyo (full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).")


def _prompt_yes_no(label: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{label} [{suffix}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def cmd_configure(args: argparse.Namespace) -> None:
    config = Config()
    existing = config.setups.get(args.name)

    print(f"Configuring setup '{args.name}'" + (" (editing existing)" if existing else "") + "\n")

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
    interval = _prompt("Seconds between frames", existing.interval if existing else 30, float)

    default_base = Path.home() / "Timelapses" / args.name
    frames_dir = _prompt(
        "Folder to save frames",
        existing.frames_dir if existing else str(default_base / "frames"),
    )
    output_dir = _prompt(
        "Folder to save finished videos",
        existing.output_dir if existing else str(default_base),
    )

    sched = existing.schedule if existing else Schedule()
    daylight = _prompt_yes_no(
        "Only capture during daylight hours (auto sunrise/sunset)?",
        sched.mode == "daylight",
    )
    if daylight:
        latitude = _prompt("Latitude (e.g. 40.4406)", sched.latitude, float)
        longitude = _prompt("Longitude (e.g. -79.9959)", sched.longitude, float)
        timezone = _prompt_timezone(
            "IANA timezone name (e.g. America/New_York)", sched.timezone
        )
        pre_offset = _prompt(
            "Start capturing this many minutes before sunrise",
            sched.pre_offset_minutes, int,
        )
        post_offset = _prompt(
            "Keep capturing this many minutes after sunset",
            sched.post_offset_minutes, int,
        )
        sched = Schedule(
            mode="daylight",
            latitude=latitude,
            longitude=longitude,
            timezone=timezone,
            pre_offset_minutes=pre_offset,
            post_offset_minutes=post_offset,
        )
    else:
        sched = Schedule(mode="always")

    setup = Setup(
        name=args.name,
        ip=ip,
        port=port,
        user=user,
        password=password,
        channel=channel,
        substream=substream,
        interval=interval,
        frames_dir=frames_dir,
        output_dir=output_dir,
        schedule=sched,
    )
    config.put(setup)
    config.save()
    print(f"\nSaved setup '{args.name}' to {config.path}")


def cmd_list(args: argparse.Namespace) -> None:
    config = Config()
    if not config.setups:
        print("No setups configured yet. Run 'reolink-timelapse configure <name>' to add one.")
        return
    for name, s in sorted(config.setups.items()):
        mode = s.schedule.mode
        print(f"{name}: {s.ip}:{s.port} channel {s.channel} "
              f"({'sub' if s.substream else 'main'} stream, every {s.interval}s) "
              f"schedule={mode}")


def cmd_remove(args: argparse.Namespace) -> None:
    config = Config()
    config.get(args.name)  # raises a clear error if missing
    config.remove(args.name)
    config.save()
    print(f"Removed setup '{args.name}'.")


def cmd_run(args: argparse.Namespace) -> None:
    config = Config()
    setup = config.get(args.name)
    run_scheduled(setup)


def cmd_build(args: argparse.Namespace) -> None:
    config = Config()
    setup = config.get(args.name)
    start_date = dt.date.fromisoformat(args.start_date) if args.start_date else None
    end_date = dt.date.fromisoformat(args.end_date) if args.end_date else None
    if args.date:
        start_date = end_date = dt.date.fromisoformat(args.date)
    out = build_timelapse(
        setup,
        output_fps=args.output_fps,
        output_file=args.output_file,
        start_date=start_date,
        end_date=end_date,
    )
    print(f"\nDone! Saved to: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="reolink-timelapse",
        description="Record and build timelapses from Reolink (or any RTSP) cameras, "
                     "across any number of camera setups.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("configure", help="Add or edit a camera setup")
    p.add_argument("name", help="Short name for this setup, e.g. 'backyard'")
    p.set_defaults(func=cmd_configure)

    p = sub.add_parser("list", help="List configured setups")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("remove", help="Remove a configured setup")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("run", help="Start capturing frames for a setup (long-running)")
    p.add_argument("name")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("build", help="Build an mp4 timelapse from captured frames")
    p.add_argument("name")
    p.add_argument("--output-fps", type=float, default=30, help="Playback fps (default: 30)")
    p.add_argument("--output-file", default=None, help="Output path (default: auto-named)")
    p.add_argument("--date", default=None, help="Only include frames from this date (YYYY-MM-DD)")
    p.add_argument("--start-date", default=None, help="Only include frames on/after this date")
    p.add_argument("--end-date", default=None, help="Only include frames on/before this date")
    p.set_defaults(func=cmd_build)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
