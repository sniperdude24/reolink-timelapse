#!/usr/bin/env python3
"""
Reolink Timelapse Frame Grabber
--------------------------------
Pulls RTSP video from an IP camera in parallel with your NVR's recording,
and saves one frame every N seconds to build a timelapse later.

Requires: ffmpeg installed and on your PATH.
  Windows: https://www.gyan.dev/ffmpeg/builds/ (download "essentials" build,
           unzip, add the /bin folder to your PATH, then open a NEW terminal)
  Verify with:  ffmpeg -version

USAGE EXAMPLES
--------------
Grab a frame every 30 seconds (default):
    python timelapse_recorder.py --ip 192.168.1.50 --user admin --password mypass

Grab a frame every 5 seconds instead:
    python timelapse_recorder.py --ip 192.168.1.50 --user admin --password mypass --interval 5

Also build the timelapse video afterward at 24fps:
    python timelapse_recorder.py --ip 192.168.1.50 --user admin --password mypass --build --output-fps 24

Just build a video from frames you already grabbed (no recording):
    python timelapse_recorder.py --build-only --output-fps 30 --frames-dir frames
"""

import argparse
import os
import subprocess
import sys
import shutil
from datetime import datetime
from urllib.parse import quote


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg was not found on your PATH.")
        print("Install it and make sure 'ffmpeg -version' works in a terminal first.")
        sys.exit(1)


def build_rtsp_url(args):
    channel = f"{args.channel:02d}"
    stream = "sub" if args.substream else "main"
    user = quote(args.user, safe="")
    password = quote(args.password, safe="")
    return f"rtsp://{user}:{password}@{args.ip}:{args.port}/h264Preview_{channel}_{stream}"


def record_frames(args):
    check_ffmpeg()
    os.makedirs(args.frames_dir, exist_ok=True)
    rtsp_url = build_rtsp_url(args)

    # fps=1/N means "1 frame every N seconds"
    fps_filter = f"fps=1/{args.interval}"
    out_pattern = os.path.join(args.frames_dir, "%Y%m%d_%H%M%S.jpg")

    cmd = [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-timeout", "10000000",  # 10s connect/read timeout (microseconds), fail fast on bad IP/creds
        "-i", rtsp_url,
        "-vf", fps_filter,
        "-q:v", "2",
        "-strftime", "1",
        out_pattern,
    ]

    print(f"Starting capture from {args.ip} (channel {args.channel}, "
          f"{'substream' if args.substream else 'main stream'})")
    print(f"Saving 1 frame every {args.interval} seconds to: {os.path.abspath(args.frames_dir)}")
    print("Press Ctrl+C to stop.\n")

    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        print("\nCapture stopped by user.")
    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg exited with an error (code {e.returncode}). "
              f"Check your IP/credentials/stream path above.")
        sys.exit(1)


def build_timelapse(args):
    check_ffmpeg()
    if not os.path.isdir(args.frames_dir) or not os.listdir(args.frames_dir):
        print(f"ERROR: no frames found in '{args.frames_dir}'. Nothing to build.")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = args.output_file or f"timelapse_{timestamp}.mp4"

    # Frames are named %Y%m%d_%H%M%S.jpg, so a plain sort is chronological order.
    frame_files = sorted(
        f for f in os.listdir(args.frames_dir) if f.lower().endswith(".jpg")
    )
    if not frame_files:
        print(f"ERROR: no .jpg frames found in '{args.frames_dir}'. Nothing to build.")
        sys.exit(1)

    # Use the concat demuxer with an explicit file list instead of
    # "-pattern_type glob", which many Windows ffmpeg builds don't support
    # (they're compiled without POSIX glob() and fail with
    # "Pattern type 'glob' is not available").
    list_path = os.path.join(args.frames_dir, "_timelapse_concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for name in frame_files:
            abs_path = os.path.abspath(os.path.join(args.frames_dir, name))
            escaped = abs_path.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-r", str(args.output_fps),
        "-i", list_path,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        out_file,
    ]

    print(f"Building timelapse at {args.output_fps} fps from {len(frame_files)} frames in '{args.frames_dir}'...")
    try:
        subprocess.run(cmd, check=True)
        print(f"\nDone! Saved to: {os.path.abspath(out_file)}")
    except subprocess.CalledProcessError as e:
        print(f"\nffmpeg exited with an error (code {e.returncode}) while building the video.")
        sys.exit(1)
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Record RTSP frames from a Reolink camera for a timelapse, and/or build the timelapse video."
    )

    # Camera connection
    parser.add_argument("--ip", help="Camera IP address (e.g. 192.168.1.50)")
    parser.add_argument("--port", type=int, default=554, help="RTSP port (default: 554)")
    parser.add_argument("--user", help="Camera username")
    parser.add_argument("--password", help="Camera password")
    parser.add_argument("--channel", type=int, default=1, help="Camera channel number (default: 1)")
    parser.add_argument("--substream", action="store_true", default=True,
                         help="Use the lower-res substream (default: on, recommended)")
    parser.add_argument("--main-stream", dest="substream", action="store_false",
                         help="Use the full-res main stream instead of substream")

    # Frame capture settings (adjustable)
    parser.add_argument("--interval", type=float, default=30,
                         help="Seconds between captured frames (default: 30)")
    parser.add_argument("--frames-dir", default="frames",
                         help="Folder to save/read frames (default: ./frames)")

    # Timelapse build settings (adjustable)
    parser.add_argument("--build", action="store_true",
                         help="After recording, also build the timelapse video")
    parser.add_argument("--build-only", action="store_true",
                         help="Skip recording, just build a video from existing frames")
    parser.add_argument("--output-fps", type=float, default=30,
                         help="Playback fps of the final timelapse video (default: 30)")
    parser.add_argument("--output-file", default=None,
                         help="Output video filename (default: timelapse_<timestamp>.mp4)")

    args = parser.parse_args()

    if args.build_only:
        build_timelapse(args)
        return

    if not args.ip or not args.user or not args.password:
        parser.error("--ip, --user, and --password are required unless using --build-only")

    record_frames(args)

    if args.build:
        build_timelapse(args)


if __name__ == "__main__":
    main()
