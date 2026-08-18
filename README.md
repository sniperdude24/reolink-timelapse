# reolink-timelapse

Turn a Reolink security camera into a time-lapse movie maker. It quietly
watches your camera's video feed and condenses hours of footage into short
videos you can actually enjoy — a whole hour becomes about a minute of
smooth, sped-up video. It keeps two movies fresh as it runs: one showing
the last hour and one covering the current six-hour stretch, which is
saved and dated before the next begins — so a full day becomes a small
library of watchable clips instead of endless raw footage. You can even
open the live view in VLC and leave it running like a self-refreshing
window into your yard. Everything is automatic: old footage is tidied up
as it goes, finished videos are named by date and time, and nothing you'd
want to keep is ever recorded over.

Under the hood it records RTSP video on your chosen schedule and drives
any number of independent **cameras** and **recordings** — different
cameras, different schedules, even several recordings off the same camera
— from one config-driven install. It speaks **Reolink's** RTSP address
format, so it works with Reolink cameras and NVR channels; other brands
would need a small change.

## Download and go (Windows)

1. Grab the newest `reolink-timelapse-win64-*.zip` from
   [Releases](https://github.com/sniperdude24/reolink-timelapse/releases)
   and unzip it anywhere.
2. Double-click `reolink-timelapse-gui.exe`. Windows SmartScreen may warn
   about an unrecognized app the first time (the exe isn't code-signed) —
   click **More info → Run anyway**.
3. Click **Add Camera**, enter your camera's (or NVR channel's) IP,
   username and password — then select it in the list and press **Start**.

That's it: the live timelapse starts building, and **Watch in VLC** opens
a self-updating view if VLC is installed.

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
  status and log output, build videos, and open your latest finished
  videos straight from the Latest Videos panel (double-click plays one),
  all from one window. No terminal needed. This is the one to hand to
  someone who isn't going to use a command line.
- **`reolink-timelapse.exe`** — the command-line version below, for
  scripting or a headless setup (e.g. a server you SSH into to check on it).

Both read/write the same `config.yaml` in that folder, so setups created in
one show up in the other. CLI usage is identical to the Python CLI below,
just swap the command:

```powershell
.\dist\reolink-timelapse\reolink-timelapse.exe configure backyard
.\dist\reolink-timelapse\reolink-timelapse.exe record backyard --camera backyard
.\dist\reolink-timelapse\reolink-timelapse.exe run backyard
.\dist\reolink-timelapse\reolink-timelapse.exe build backyard
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

Add a recording against it — interval and capture schedule (frames and
videos save automatically under the program folder, see
[Where things are stored](#where-things-are-stored)):

```bash
python -m reolink_timelapse record backyard --camera backyard
```

Start capturing (long-running — leave it running, e.g. in a scheduled task
or a terminal you don't close):

```bash
python -m reolink_timelapse run backyard
```

Rebuild the newest recorded session's video (a lossless rejoin of its
clips — sessions also build themselves automatically when they end):

```bash
python -m reolink_timelapse build backyard
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

## Pacing and video length

Each recording has an **output video fps** (default 30) used for its
automatically built session videos and pre-filled when building manually.
Capture pacing works one of two ways:

- **Choose a video length**: set how long you want the finished video to
  be (e.g. 60 seconds) and the dialog shows the **recommended capture
  rate** live ("1 frame every 24.00s") for the current schedule and fps.
  Saving keeps it auto-adjusting: the rate is re-derived at the start of
  every session from that day's actual window, so daylight videos stay the
  target length even as sunrise/sunset drift with the seasons. Or click
  **"Use as fixed interval"** to lock today's recommended rate into the
  interval field instead. Needs a schedule with a defined session length
  (Daylight hours, Fixed daily time, or Timer — not Always).
- **Set the interval manually**: one frame every N seconds. The dialog
  shows a live estimate of how long the finished video will come out for
  the current schedule, interval, and fps.

## Live rolling timelapse

The **Live Timelapse** panel is the main screen. It lists every camera
you've added, with Start / Stop, Play Last Hour, Play Session and Open
Folder acting on whichever camera is selected — plus Add Camera / Edit /
Remove, so the camera list and the live controls are the same list.

**Any number of cameras can run at once**, each with its own row showing
live status, chunk count, segment size and last-update time. As each
camera's chunks complete they're folded into two always-current **1080p
60fps** videos at 60x speed — one video frame per real second, so 5
minutes of real time plays in ~5 seconds:

- **`last_hour.mp4`** — the trailing hour, playing in about a minute; the
  oldest 5 minutes falls off as each new chunk arrives. Rebuilt every
  chunk.
- **`session.mp4`** — the session in progress, rebuilt every ~15 minutes.

Both live under `Timelapses\Live\<camera>\` and update atomically — the
view lags real time by at most one chunk; just re-open the file to see the
latest.

### Sessions roll over every 6 hours

A camera left running for days shouldn't produce one video that grows
forever. Every 6 hours the session is closed off, renamed into
`Timelapses\Live\<camera>\sessions\` as
`<camera>_<date>_<start>-<end>.mp4`, and a fresh one starts — so you get a
series of watchable ~6-minute timelapses instead of an unopenable monolith.
Closing a session is a rename, not a re-encode, so it costs nothing. Each
completed block appears in **Latest Videos**.

**Stopping archives everything watchable.** Stop (or closing the program)
closes off the partial session the same way, and also renames the final
`last_hour.mp4` into `sessions\` as `<camera>_<date>_<start>-<end>_lasthour.mp4`
— so the next Start never overwrites footage you might still want. If the
program dies without a clean stop, whatever it left behind is preserved as
`*_recovered.mp4` on the next start.

### Watch live in VLC (self-updating)

The **Watch in VLC** button opens the selected camera's last hour in VLC
on a loop, served by a small built-in web server (localhost only —
nothing is exposed to your network). Because it's looped over HTTP, every
replay re-fetches the file: the view is about a minute long and refreshes
itself with the newest footage on each pass, no clicking re-open. If VLC
isn't installed where the app can find it, a dialog shows the address to
paste into VLC's *Media > Open Network Stream* (turn on Loop).

Watching over HTTP also means VLC never holds a lock on the file itself,
so it can't stall the live refresh the way a player opening
`last_hour.mp4` directly can.

That cap is also what keeps `session.mp4` current: rebuilding it is a full
re-mux, so on an uncapped session the cost grows with the square of how
long it's been running. Bounded to 6 hours, a 15-minute refresh costs about
8 GB of writes a day.

From the CLI:

```bash
python -m reolink_timelapse live --camera backyard
```

**Running several cameras.** Capturing is a stream copy and costs almost
nothing (~1% of one core per camera); the work is in rendering, about
0.31 cores sustained per 4K camera. Nothing uses the GPU. Rendering does
arrive in bursts, so conversions across all cameras are **serialised** —
they queue rather than stacking and spiking the machine. Three 4K cameras
need roughly 165 seconds of rendering per 5-minute window, so there's
plenty of headroom.

Disk stays flat no matter how long it runs: each raw chunk is **deleted
as soon as it's been converted** and stitched into both videos (kept only
if its conversion fails), the small 1080p segments are kept for the
current session so its videos could be rebuilt, and starting a new
session clears the previous session's segments. Budget roughly 30 MB per
hour per camera of kept segments in daylight (much less at night, when
the dark scene compresses far better), plus one transient chunk of about
1.8 GB/hour that never accumulates.

## Sessions, folders, and videos

Every continuous start-to-stop capture run is a **session** — one recurring
window (Daylight hours / Fixed daily time), one Timer run, or one manual
start/stop in "Always" mode. Each session gets its own subfolder named from
its start time plus a short random suffix (e.g. `20260816_064712_a1b2`), so
two sessions can never collide — including two recordings sharing a camera,
or two instances of the same recording started at the same time.

### How capture works

Recording ingests **video**, not individual snapshots. One ffmpeg
stream-copies the camera's feed into short `.ts` chunks — no decoding, so
capture itself costs almost no CPU. Each completed chunk is immediately
rendered into a small sped-up clip and **the raw chunk is deleted**, so
disk stays flat: only the chunk currently recording and the one being
rendered are ever on disk at once (a chunk is roughly 1.8 GB/hour of 4K
while it exists).

Rendering as you go matters for long recordings: a 14-hour session rendered
in one go at the end would take about two hours of decoding. Spreading it
across the session means the finished video is ready seconds after the
window closes.

The session's video is then a fast, lossless join of those clips, named
`<recording>_<date>_<start>-<end>.mp4` (24-hour clock, e.g.
`backyard_2026-08-16_06h47-20h54.mp4`) and written to the recording's
folder, which stays flat — only the working files get split per session.

**Rebuilding.** "Build Video..." (or `build <name>`) rejoins a session's
clips losslessly. The capture interval and fps were fixed when the session
was recorded, so there's nothing to configure beyond picking the session.
(Frame-by-frame JPEG sessions from v0.11 and older are no longer
buildable by this version — use the old version's exe alongside them if
one ever needs rebuilding; their already-built videos play fine.)

## Where things are stored

Everything auto-saves into one **program folder** — the exe's own folder
for the packaged build, or the repo root for a source run — so a whole
install (app + settings + captured media) can be copied to another machine
or a USB drive as a single unit. There's nothing to configure:

- **Config** (camera credentials, and each recording's schedule) lives in
  `config.yaml` in the program folder, storing cameras and recordings as
  two separate lists so multiple recordings can share one camera. If
  you're upgrading from an older version, the first run automatically
  migrates forward any existing config it finds — nothing already
  configured gets lost.
- **Working files** live in
  `<program folder>\Timelapses\<recording-name>\sessions\<session>\` —
  `chunks\` (transient, deleted as each is rendered) and `segments\` (the
  small rendered clips, kept so the video can be rejoined).
- **Finished videos** save to `<program folder>\Timelapses\<recording-name>\`
  — always, derived from the recording's name. (Older versions let these
  be customized per recording; any previously customized folders — and old
  `frames\` folders — are left in place on disk but are no longer read or
  written.)

Note for source runs: `config.yaml` (which holds plaintext camera
credentials) ends up sitting directly in the repo working directory. It's
gitignored, but worth knowing if that repo checkout isn't otherwise private.

## Notes / known limitations

- Passwords are stored in the config file in plaintext (file permissions are
  set to owner-only on macOS/Linux; Windows relies on your user account's
  normal file ACLs). Fine for personal/local use. OS keychain storage via
  `keyring` is a reasonable future improvement if this needs to be more
  robust.
- Chunks and clips are named by timestamp, so they sort chronologically
  for free.
- **If you see corruption, check the source first.** Streaming the same
  camera twice at once (say, an NVR recording it *and* this app pulling
  it directly) can push the camera past what it can reliably send, and it
  drops slices to keep up. The damage is baked into the stream before this
  app ever sees it, so no amount of processing here can undo it. If the
  camera is attached to an NVR, point the recording at the **NVR's**
  channel instead — Reolink NVRs re-serve each channel over RTSP with the
  same URL shape (`h264Preview_<channel>_main`), usually as a
  pass-through, so you get identical quality with one less consumer on the
  camera. Measured on the author's setup: the direct feed produced regular
  bursts of damage, the same camera via the NVR produced none.
- Two protections handle what does slip through. Packets ffmpeg flags as
  corrupt are dropped (`-fflags discardcorrupt`), and capture intervals of
  5 seconds or more keep only **keyframes** — the self-contained frames
  the camera sends every couple of seconds, which can't inherit smearing
  from earlier damage. Neither shortens your video meaningfully; a slot
  just gets the next clean frame.
- Decoding is deliberately software-only. GPU decode (NVDEC) was tried and
  rejected: on Reolink's tiled HEVC it mis-stitches tile boundaries into a
  vivid-green vertical line — decoding the same recording twice gave 0
  damaged frames in software versus 401 with NVDEC. GPU *encoding* (NVENC)
  was also measured and rejected: it saves about 5% CPU while making files
  4x larger.
