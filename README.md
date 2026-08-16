# reolink-timelapse

Records RTSP frames from an IP camera (Reolink or otherwise) in parallel with
your NVR's own recording, only during your chosen schedule, and stitches them
into a timelapse video. Built to drive any number of independent **cameras**
and **recordings** — different cameras, different locations, different
schedules, even several recordings off the same camera — from one
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

(needs ffmpeg on PATH *at build time only*, to copy into the bundle — the
result doesn't need it afterward). This produces **one** self-contained
folder, `dist\reolink-timelapse\`, holding both exes plus a single shared
`ffmpeg.exe` — zip the whole folder up and it runs anywhere, with config,
frames, and videos also defaulting to inside that same folder (see
[Where things are stored](#where-things-are-stored)):

- **`reolink-timelapse-gui.exe`** — double-click for the graphical control
  panel: add cameras, add scheduled recordings against them (with a
  click-to-pick location map and a timezone dropdown you can also
  auto-detect from the PC's own settings), start/stop capture, see live
  status and log output, and build videos, all from one window. No terminal
  needed. This is the one to hand to someone who isn't going to use a
  command line.
- **`reolink-timelapse.exe`** — the command-line version below, for
  scripting or a headless setup (e.g. a server you SSH into to check on it).

Both read/write the same `config.yaml` in that folder, so setups created in
one show up in the other. CLI usage is identical to the Python CLI below,
just swap the command:

```powershell
.\dist\reolink-timelapse\reolink-timelapse.exe configure backyard
.\dist\reolink-timelapse\reolink-timelapse.exe record backyard --camera backyard
.\dist\reolink-timelapse\reolink-timelapse.exe run backyard
.\dist\reolink-timelapse\reolink-timelapse.exe build backyard --output-fps 24
```

## Install (from source)

```bash
pip install -r requirements.txt
```

(or `pip install -e .` to also get the `reolink-timelapse` command on your PATH)

## Quick start

Cameras and recordings are separate: a **camera** is just a reusable RTSP
source (IP/credentials/channel), and a **recording** is a schedule/output
configuration that points at one. The same camera can back more than one
recording (e.g. two different schedules off one camera).

Add a camera — connection details only:

```bash
python -m reolink_timelapse configure backyard
```

Add a recording against it — where to store frames/videos and the capture
schedule:

```bash
python -m reolink_timelapse record backyard --camera backyard
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

List, or remove a camera/recording:

```bash
python -m reolink_timelapse list
python -m reolink_timelapse remove-recording backyard
python -m reolink_timelapse remove-camera backyard
```

(a camera can't be removed while a recording still references it)

If you installed with `pip install -e .`, drop the `python -m reolink_timelapse`
prefix and just run `reolink-timelapse configure backyard`, etc.

Or skip the individual commands and use the graphical control panel instead
(same underlying engine, just a window instead of a terminal):

```bash
python -m reolink_timelapse gui
```

## How scheduling works

Each recording runs in one of four modes:

- **Daylight hours** (recommended for outdoor timelapses): give a
  latitude/longitude (typed, or picked from the GUI's map) and IANA timezone
  name (e.g. `America/New_York`, pickable from a dropdown or auto-detected
  from the PC's own timezone setting in the GUI) once, and the tool computes
  that day's actual sunrise/sunset every day via the `astral` library — no
  network calls, no API keys. Optional pre/post offset minutes widen the
  window (e.g. start 30 min before sunrise). The window automatically shifts
  with the seasons.
- **Fixed daily time**: the same daily-recurring idea as Daylight hours, but
  with clock times you set yourself (e.g. every day 7:00 AM-6:00 PM) instead
  of computed sunrise/sunset. Supports overnight windows (e.g. 22:00-02:00).
- **Timer**: a manual one-shot — pressing Start begins capture immediately
  and it stops itself after a set number of minutes, with no daily repeat.
- **Always**: capture runs continuously with no schedule, until stopped.

`run <name>` is meant to be left running long-term for the recurring modes
(Daylight hours / Fixed daily time): outside the capture window it sleeps
until the next window start, and if ffmpeg exits unexpectedly mid-window
(camera reboot, network blip) it waits 30s and retries automatically rather
than giving up.

## Sessions, folders, and videos

Every continuous start-to-stop capture run is a **session** — one recurring
window (Daylight hours / Fixed daily time), one Timer run, or one manual
start/stop in "Always" mode. Each session gets its own subfolder under
`frames_dir`, named from its start time plus a short random suffix (e.g.
`20260816_064712_a1b2`), so two sessions can never collide — including two
recordings sharing a camera, or two instances of the same recording started
at the same time.

A video is **built automatically right after each session ends** — window
close, timer elapsing, manual stop, or Ctrl+C — named
`<recording>_<date>_<start>-<end>.mp4` (24-hour clock, e.g.
`backyard_2026-08-16_06h47-20h54.mp4`) and written to `output_dir`, which
stays a single flat folder shared by every session's video — only the frames
get split up, not the finished videos. `build <name>` (or the GUI's "Build
Video...") still works too, and aggregates frames across *all* of a
recording's sessions (plus any frames captured before per-session folders
existed) unless you filter by date.

## Where things are stored

Config, frames, and videos all default into one **program folder** — the
exe's own folder for the packaged build, or the repo root for a source run
— so a whole install (app + settings + captured media) can be copied to
another machine or a USB drive as a single unit:

- **Config** (camera credentials, and each recording's schedule/output
  settings) defaults to `config.yaml` in the program folder, storing
  cameras and recordings as two separate lists so multiple recordings can
  share one camera. If you're upgrading from an older version, the first
  run automatically migrates forward any existing config it finds — both
  the older single-camera-per-setup schema (each becomes one camera plus
  one recording of the same name) and, before that, the old config location
  (`%APPDATA%\reolink-timelapse\config.yaml` on Windows,
  `~/.config/reolink-timelapse/config.yaml` elsewhere) — nothing already
  configured gets lost.
- **Frames** default to `<program folder>\Timelapses\<recording-name>\frames\<session>\`,
  and **finished videos** to `<program folder>\Timelapses\<recording-name>\`.

Both are just defaults, suggested during `record`/the GUI's Add Recording
dialog — point `frames_dir`/`output_dir` at a separate drive per recording
if you'd rather keep media outside the program folder.

Note for source runs: `config.yaml` (which holds plaintext camera
credentials) ends up sitting directly in the repo working directory. It's
gitignored, but worth knowing if that repo checkout isn't otherwise private.

## Notes / known limitations

- Passwords are stored in the config file in plaintext (file permissions are
  set to owner-only on macOS/Linux; Windows relies on your user account's
  normal file ACLs). Fine for personal/local use. OS keychain storage via
  `keyring` is a reasonable future improvement if this needs to be more
  robust.
- Frames are named by timestamp (`%Y%m%d_%H%M%S.jpg`), so they sort
  chronologically for free and `build --date` can filter by day.
