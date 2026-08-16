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

`dist\reolink-timelapse\reolink-timelapse.exe` is a self-contained build —
Python and ffmpeg are both bundled inside the `dist\reolink-timelapse\`
folder, so it runs with nothing else installed. Build it yourself:

```powershell
.\build_exe.ps1
```

(needs ffmpeg on PATH *at build time only*, to copy into the bundle — the
resulting exe doesn't need it afterward). This produces `dist\reolink-timelapse\`,
which is the whole distributable — zip that folder up and it'll run anywhere.

Usage is identical to the Python CLI below, just swap the command:

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

## Where things are stored

- **Config** (camera credentials, per-setup schedule) lives in your OS user
  config dir — `%APPDATA%\reolink-timelapse\config.yaml` on Windows,
  `~/.config/reolink-timelapse/config.yaml` elsewhere — not inside this repo,
  so it's never at risk of being committed.
- **Frames and finished videos** default to `~/Timelapses/<setup-name>/`,
  configurable per setup during `configure`.

## Notes / known limitations

- Passwords are stored in the config file in plaintext (file permissions are
  set to owner-only on macOS/Linux; Windows relies on your user account's
  normal file ACLs). Fine for personal/local use. OS keychain storage via
  `keyring` is a reasonable future improvement if this needs to be more
  robust.
- Frames are named by timestamp (`%Y%m%d_%H%M%S.jpg`), so they sort
  chronologically for free and `build --date` can filter by day.
