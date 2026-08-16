# reolink-timelapse

Records RTSP frames from an IP camera (Reolink or otherwise) in parallel with
your NVR's own recording, only during daylight hours, and stitches them into
a timelapse video. Built to drive any number of independent camera setups —
different cameras, different locations, different schedules — from one
config-driven install rather than a one-off script per camera.

## Requirements

- Python 3.9+
- [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) on your PATH (`ffmpeg -version` should work)

...or skip both and use the standalone Windows .exe build below, which bundles
its own Python and ffmpeg.

## Standalone .exe (Windows)

Build it yourself:

```powershell
.\build_exe.ps1
```

(needs ffmpeg on PATH *at build time only*, to copy into the bundle — neither
resulting exe needs it afterward). This produces two self-contained builds,
each with Python and ffmpeg bundled inside so they run with nothing else
installed — zip either folder up and it runs anywhere:

- **`dist\reolink-timelapse-gui\reolink-timelapse-gui.exe`** — double-click
  for the graphical control panel: add/edit/remove setups, start/stop
  capture, see live status and log output, and build videos, all from one
  window. No terminal needed. This is the one to hand to someone who isn't
  going to use a command line.
- **`dist\reolink-timelapse\reolink-timelapse.exe`** — the command-line
  version below, for scripting or a headless setup (e.g. a server you SSH
  into to check on it).

CLI usage is identical to the Python CLI below, just swap the command:

```powershell
.\dist\reolink-timelapse\reolink-timelapse.exe configure backyard
.\dist\reolink-timelapse\reolink-timelapse.exe run backyard
.\dist\reolink-timelapse\reolink-timelapse.exe build backyard --output-fps 24
```

## Install (from source)

```bash
pip install -r requirements.txt
```

(or `pip install -e .` to also get the `reolink-timelapse` command on your PATH)

## Quick start

Add a setup — this walks you through camera connection details, where to
store frames/videos, and the capture schedule, then saves it under a name
you choose:

```bash
python -m reolink_timelapse configure backyard
```

Start capturing (long-running — leave it running, e.g. in a scheduled task
or a terminal you don't close):

```bash
python -m reolink_timelapse run backyard
```

Build a video from what's been captured so far:

```bash
python -m reolink_timelapse build backyard --output-fps 24
```

Build just one day's video:

```bash
python -m reolink_timelapse build backyard --date 2026-08-15
```

List or remove setups:

```bash
python -m reolink_timelapse list
python -m reolink_timelapse remove backyard
```

If you installed with `pip install -e .`, drop the `python -m reolink_timelapse`
prefix and just run `reolink-timelapse configure backyard`, etc.

Or skip the individual commands and use the graphical control panel instead
(same underlying engine, just a window instead of a terminal):

```bash
python -m reolink_timelapse gui
```

## How scheduling works

Each setup can run in one of two modes (chosen during `configure`):

- **Daylight hours** (recommended for outdoor timelapses): give a
  latitude/longitude and IANA timezone name (e.g. `America/New_York`) once,
  and the tool computes that day's actual sunrise/sunset every day via the
  `astral` library — no network calls, no API keys. Optional pre/post offset
  minutes widen the window (e.g. start 30 min before sunrise). The window
  automatically shifts with the seasons.
- **Always**: capture runs continuously with no schedule.

`run <name>` is meant to be left running long-term: outside the capture
window it sleeps until the next window start, and if ffmpeg exits
unexpectedly mid-window (camera reboot, network blip) it waits 30s and
retries automatically rather than giving up.

## Sessions, folders, and videos

Every continuous start-to-stop capture run is a **session** — one daylight
window, or one manual start/stop in "always" mode. Each session gets its own
subfolder under `frames_dir`, named from its start time plus a short random
suffix (e.g. `20260816_064712_a1b2`), so two sessions can never collide —
including two instances of the same setup started at the same time.

A video is **built automatically right after each session ends** — window
close, manual stop, or Ctrl+C — named `<setup>_<date>_<start>-<end>.mp4`
(24-hour clock, e.g. `backyard_2026-08-16_06h47-20h54.mp4`) and written to
`output_dir`, which stays a single flat folder shared by every session's
video — only the frames get split up, not the finished videos. `build
<name>` (or the GUI's "Build Video...") still works too, and aggregates
frames across *all* of a setup's sessions (plus any frames captured before
per-session folders existed) unless you filter by date.

## Where things are stored

- **Config** (camera credentials, per-setup schedule) lives in your OS user
  config dir — `%APPDATA%\reolink-timelapse\config.yaml` on Windows,
  `~/.config/reolink-timelapse/config.yaml` elsewhere — not inside this repo,
  so it's never at risk of being committed.
- **Frames** default to `~/Timelapses/<setup-name>/frames/<session>/`, and
  **finished videos** to `~/Timelapses/<setup-name>/`, both configurable per
  setup during `configure`.

## Notes / known limitations

- Passwords are stored in the config file in plaintext (file permissions are
  set to owner-only on macOS/Linux; Windows relies on your user account's
  normal file ACLs). Fine for personal/local use. OS keychain storage via
  `keyring` is a reasonable future improvement if this needs to be more
  robust.
- Frames are named by timestamp (`%Y%m%d_%H%M%S.jpg`), so they sort
  chronologically for free and `build --date` can filter by day.
