"""Tkinter control panel: add/edit/remove cameras and recordings, start/stop
capture, build videos."""

from __future__ import annotations

import datetime as dt
import os
import queue
import shutil
import subprocess
import threading
import tkinter as tk
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional
from zoneinfo import available_timezones

import tzlocal

from .build import build_timelapse
from .chunks import refresh_output
from .config import Camera, Config, Recording, Schedule, Setup, is_valid_timezone
from .live import live_dirs, run_live
from .rtsp import no_console_kwargs
from .scheduler import daylight_window, next_window, run_scheduled
from .webstream import start_stream_server, stream_url

_TIMEZONES = sorted(available_timezones())


def _parse_optional_float(raw: str) -> Optional[float]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_optional_int(raw: str, default: int = 0) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_optional_int_or_none(raw: str) -> Optional[int]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _valid_hhmm(raw: str) -> bool:
    try:
        dt.datetime.strptime(raw, "%H:%M")
        return True
    except ValueError:
        return False


def _fmt_time(t: dt.datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


@dataclass
class RunningCapture:
    thread: threading.Thread
    stop_event: threading.Event


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Reolink Timelapse")
        root.geometry("1200x620")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.config = Config()
        self.running: dict[str, RunningCapture] = {}
        # Any number of cameras can run live at once; both dicts are keyed
        # by camera name. Worker threads only ever write live_state[name],
        # the UI loop only reads, so no locking is needed.
        self.live_workers: dict[str, RunningCapture] = {}
        self.live_state: dict[str, dict] = {}
        self.vlc_procs: dict[str, object] = {}
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self._closed = False
        self._closing = False
        self._window_refresh_tick = 0
        self._videos_refresh_tick = 0
        self._videos_cache: list = []

        self._build_widgets()
        start_stream_server(log=self.log)
        if self.config.migrated_from:
            self.log(f"Migrated existing config from {self.config.migrated_from} to {self.config.path}.")
        if self.config.migration_error:
            self.log(f"WARNING: {self.config.migration_error}")
        if self.config.schema_migrated:
            self.log("Upgraded config.yaml to the newer cameras/recordings format.")
        self.refresh_lists()
        self.root.after(200, self._drain_log_queue)
        self.root.after(1000, self._refresh_status_loop)

    # ---- layout ----

    def _build_widgets(self) -> None:
        content = ttk.Frame(self.root)
        content.pack(fill="both", expand=True)
        left = ttk.Frame(content)
        left.pack(side="left", fill="both", expand=True)

        right = ttk.Frame(content)
        right.pack(side="right", fill="y", padx=(4, 8), pady=8)

        videos_frame = ttk.LabelFrame(right, text="Latest Videos")
        videos_frame.pack(fill="both", expand=True)
        self._videos_frame = videos_frame
        self.videos_tree = ttk.Treeview(
            videos_frame, columns=("recording", "created"), show="tree headings", height=15
        )
        self.videos_tree.heading("#0", text="Video")
        self.videos_tree.heading("recording", text="Recording")
        self.videos_tree.heading("created", text="Created")
        self.videos_tree.column("#0", width=250)
        self.videos_tree.column("recording", width=90)
        self.videos_tree.column("created", width=110)
        self.videos_tree.pack(fill="both", expand=True, padx=4, pady=(4, 2))
        self.videos_tree.bind("<Double-1>", self.on_play_video)
        videos_toolbar = ttk.Frame(videos_frame)
        videos_toolbar.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(videos_toolbar, text="Play", command=self.on_play_video).pack(side="left", padx=2)
        ttk.Button(videos_toolbar, text="Show in Folder", command=self.on_show_video_folder).pack(side="left", padx=2)

        # Live Timelapse is the main feature: it gets the top of the left
        # column and the camera list itself, so there's no separate
        # "Added Cameras" section to keep in sync.
        live_frame = ttk.LabelFrame(left, text="Live Timelapse")
        live_frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        live_run = ttk.Frame(live_frame)
        live_run.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Button(live_run, text="Start", command=self.on_live_start).pack(side="left", padx=2)
        ttk.Button(live_run, text="Stop", command=self.on_live_stop).pack(side="left", padx=2)
        ttk.Separator(live_run, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(live_run, text="Play Last Hour",
                   command=lambda: self.on_play_live("last_hour.mp4")).pack(side="left", padx=2)
        ttk.Button(live_run, text="Play Session",
                   command=lambda: self.on_play_live("session.mp4")).pack(side="left", padx=2)
        ttk.Button(live_run, text="Watch in VLC",
                   command=self.on_watch_live_vlc).pack(side="left", padx=2)
        ttk.Button(live_run, text="Open Folder",
                   command=self.on_open_live_folder).pack(side="left", padx=2)

        live_manage = ttk.Frame(live_frame)
        live_manage.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(live_manage, text="Add Camera", command=self.on_add_camera).pack(side="left", padx=2)
        ttk.Button(live_manage, text="Edit", command=self.on_edit_camera).pack(side="left", padx=2)
        ttk.Button(live_manage, text="Remove", command=self.on_remove_camera).pack(side="left", padx=2)

        self.camera_tree = ttk.Treeview(
            live_frame, columns=("address", "status", "chunks", "segments", "updated"),
            show="tree headings", height=8,
        )
        self.camera_tree.heading("#0", text="Camera")
        self.camera_tree.heading("address", text="Address")
        self.camera_tree.heading("status", text="Status")
        self.camera_tree.heading("chunks", text="Chunks")
        self.camera_tree.heading("segments", text="Segments")
        self.camera_tree.heading("updated", text="Updated")
        self.camera_tree.column("#0", width=140)
        self.camera_tree.column("address", width=180)
        self.camera_tree.column("status", width=80, anchor="center")
        self.camera_tree.column("chunks", width=70, anchor="e")
        self.camera_tree.column("segments", width=90, anchor="e")
        self.camera_tree.column("updated", width=90, anchor="center")
        self.camera_tree.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.camera_tree.bind("<Double-1>",
                              lambda _e: self.on_play_live("last_hour.mp4"))

        recordings_frame = ttk.LabelFrame(left, text="Scheduled Recordings")
        recordings_frame.pack(fill="x", padx=8, pady=(4, 4))

        recordings_toolbar = ttk.Frame(recordings_frame)
        recordings_toolbar.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Button(recordings_toolbar, text="Add Recording", command=self.on_add_recording).pack(side="left", padx=2)
        ttk.Button(recordings_toolbar, text="Edit", command=self.on_edit_recording).pack(side="left", padx=2)
        ttk.Button(recordings_toolbar, text="Remove", command=self.on_remove_recording).pack(side="left", padx=2)

        recordings_toolbar2 = ttk.Frame(recordings_frame)
        recordings_toolbar2.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(recordings_toolbar2, text="Start", command=self.on_start).pack(side="left", padx=2)
        ttk.Button(recordings_toolbar2, text="Stop", command=self.on_stop).pack(side="left", padx=2)
        ttk.Button(recordings_toolbar2, text="Build Video...", command=self.on_build).pack(side="left", padx=2)
        ttk.Button(recordings_toolbar2, text="Open Folder", command=self.on_open_folder).pack(side="left", padx=2)

        self.recording_tree = ttk.Treeview(
            recordings_frame, columns=("camera", "mode", "window", "status"),
            show="tree headings", height=5
        )
        self.recording_tree.heading("#0", text="Recording")
        self.recording_tree.heading("camera", text="Camera")
        self.recording_tree.heading("mode", text="Schedule")
        self.recording_tree.heading("window", text="Window")
        self.recording_tree.heading("status", text="Status")
        self.recording_tree.column("#0", width=120)
        self.recording_tree.column("camera", width=100)
        self.recording_tree.column("mode", width=90)
        self.recording_tree.column("window", width=220)
        self.recording_tree.column("status", width=90)
        self.recording_tree.pack(fill="x", padx=4, pady=(0, 4))

        log_frame = ttk.LabelFrame(left, text="Log")
        log_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log_text = tk.Text(log_frame, height=16, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # ---- lists ----

    def refresh_lists(self) -> None:
        self._refresh_camera_tree()
        self._refresh_recording_tree()
        self._refresh_videos_tree()

    def _scan_latest_videos(self) -> list:
        """[(mtime, path, source_name)] for the newest ~20 finished videos,
        oldest first so the newest sits at the bottom of the list.

        Covers both sources: each recording's output folder, and the dated
        blocks a live session closes off into Timelapses/Live/<camera>/
        sessions/ -- without the latter, completed live timelapses would
        appear nowhere in the UI.
        """
        entries = []
        for name, r in self.config.recordings.items():
            out = Path(r.output_dir)
            if not out.is_dir():
                continue
            for p in out.glob("*.mp4"):
                try:
                    entries.append((p.stat().st_mtime, str(p), name))
                except OSError:
                    continue
        for name in self.config.cameras:
            blocks = live_dirs(name)[2] / "sessions"
            if not blocks.is_dir():
                continue
            for p in blocks.glob("*.mp4"):
                try:
                    entries.append((p.stat().st_mtime, str(p), f"{name} (live)"))
                except OSError:
                    continue
        entries.sort(reverse=True)
        return entries[:20][::-1]

    def _refresh_videos_tree(self) -> None:
        # Rebuild only when the file set actually changed, so the panel
        # updates within a tick of a new build finishing without churning
        # selection every second.
        entries = self._scan_latest_videos()
        if entries == self._videos_cache:
            return
        self._videos_cache = entries
        selected = self._tree_selection(self.videos_tree)
        self.videos_tree.delete(*self.videos_tree.get_children())
        for mtime, path, name in entries:
            created = dt.datetime.fromtimestamp(mtime).strftime("%b %d %I:%M %p")
            self.videos_tree.insert("", "end", iid=path, text=os.path.basename(path),
                                     values=(name, created))
        if selected and self.videos_tree.exists(selected):
            self.videos_tree.selection_set(selected)

    def selected_video(self) -> Optional[str]:
        return self._tree_selection(self.videos_tree)

    def on_play_video(self, _event=None) -> None:
        path = self.selected_video()
        if not path:
            messagebox.showinfo("Play Video", "Select a video first.")
            return
        if not os.path.exists(path):
            messagebox.showerror("Play Video", "That video no longer exists.")
            self._refresh_videos_tree()
            return
        os.startfile(path)  # Windows only, matches this project's target platform

    def on_show_video_folder(self) -> None:
        path = self.selected_video()
        if not path:
            messagebox.showinfo("Show in Folder", "Select a video first.")
            return
        os.startfile(os.path.dirname(path))

    def _live_cells(self, name: str) -> tuple:
        """(status, chunks, segments, updated) for one camera's live row."""
        if not self._live_running(name):
            return ("Off", "", "", "")
        st = self.live_state.get(name) or {}
        if not st.get("updated"):
            return ("Live", "...", "", "")
        return ("Live", str(st["chunks"]), f"{st['mb']:.0f} MB",
                st["updated"].strftime("%H:%M:%S"))

    def _refresh_camera_tree(self) -> None:
        selected = self._tree_selection(self.camera_tree)
        self.camera_tree.delete(*self.camera_tree.get_children())
        for name, c in sorted(self.config.cameras.items()):
            stream = "sub" if c.substream else "main"
            self.camera_tree.insert("", "end", iid=name, text=name, values=(
                f"{c.ip}:{c.port} ch{c.channel:02d} {stream}",
                *self._live_cells(name),
            ))
        if selected and self.camera_tree.exists(selected):
            self.camera_tree.selection_set(selected)

    def _refresh_recording_tree(self) -> None:
        selected = self._tree_selection(self.recording_tree)
        self.recording_tree.delete(*self.recording_tree.get_children())
        for name, r in sorted(self.config.recordings.items()):
            status = "Running" if name in self.running else "Stopped"
            self.recording_tree.insert("", "end", iid=name, text=name, values=(
                r.camera_name,
                r.schedule.mode,
                self._window_label(r),
                status,
            ))
        if selected and self.recording_tree.exists(selected):
            self.recording_tree.selection_set(selected)

    def _window_label(self, r: Recording) -> str:
        mode = r.schedule.mode
        if mode == "always":
            return "Continuous"
        if mode == "duration":
            return f"Timer: {r.schedule.duration_minutes} min from Start"
        try:
            # next_window() only reads setup.schedule -- a Recording carries
            # its own Schedule directly, so it works here without resolving
            # to a full Setup (no camera needed just to preview the window).
            window = next_window(r)
        except Exception:
            return "(check schedule settings)"
        now = dt.datetime.now(window.start.tzinfo)
        if window.start.date() == now.date():
            day_label = "Today"
        elif window.start.date() == now.date() + dt.timedelta(days=1):
            day_label = "Tomorrow"
        else:
            day_label = window.start.strftime("%b %d")
        return f"{day_label} {_fmt_time(window.start)}-{_fmt_time(window.end)}"

    def _refresh_status_loop(self) -> None:
        # Only the Status cells change tick-to-tick, so update those in
        # place rather than rebuilding both trees every second (which
        # churned selection and recomputed sunrise/sunset per row per tick).
        # Window labels drift once a day; refreshing them once a minute is
        # plenty. Full refresh_lists() still runs on add/edit/remove/start.
        if self._closed:
            return
        for name in list(self.running):
            if not self.running[name].thread.is_alive():
                del self.running[name]
        for name in list(self.live_workers):
            if not self.live_workers[name].thread.is_alive():
                del self.live_workers[name]
        for iid in self.recording_tree.get_children():
            status = "Running" if iid in self.running else "Stopped"
            if self.recording_tree.set(iid, "status") != status:
                self.recording_tree.set(iid, "status", status)
        self._videos_refresh_tick += 1
        if self._videos_refresh_tick >= 5:
            # Videos appear every few minutes at best; scanning every
            # recording's folder once a second is wasted disk I/O.
            self._videos_refresh_tick = 0
            self._refresh_videos_tree()
        self._window_refresh_tick += 1
        if self._window_refresh_tick >= 60:
            self._window_refresh_tick = 0
            for iid in self.recording_tree.get_children():
                recording = self.config.recordings.get(iid)
                if recording:
                    self.recording_tree.set(iid, "window", self._window_label(recording))
        self._update_live_status()
        self.root.after(1000, self._refresh_status_loop)

    def _tree_selection(self, tree: ttk.Treeview) -> Optional[str]:
        sel = tree.selection()
        return sel[0] if sel else None

    def selected_camera(self) -> Optional[Camera]:
        name = self._tree_selection(self.camera_tree)
        return self.config.cameras.get(name) if name else None

    def selected_recording(self) -> Optional[Recording]:
        name = self._tree_selection(self.recording_tree)
        return self.config.recordings.get(name) if name else None

    # ---- logging (thread-safe: workers push, only the main thread touches the widget) ----

    def log(self, msg: str) -> None:
        # Timestamp when the event happened, not when the queue drains.
        self.log_queue.put(f"[{dt.datetime.now():%H:%M:%S}] {msg}")

    def _drain_log_queue(self) -> None:
        if self._closed:
            return
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg.rstrip("\n") + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(200, self._drain_log_queue)

    # ---- camera actions ----

    def on_add_camera(self) -> None:
        CameraDialog(self.root, self.config, None, self.refresh_lists)

    def on_edit_camera(self) -> None:
        camera = self.selected_camera()
        if not camera:
            messagebox.showinfo("Edit Camera", "Select a camera first.")
            return
        CameraDialog(self.root, self.config, camera, self.refresh_lists)

    def on_remove_camera(self) -> None:
        name = self._tree_selection(self.camera_tree)
        if not name:
            messagebox.showinfo("Remove Camera", "Select a camera first.")
            return
        if self._live_running(name):
            messagebox.showwarning(
                "Remove Camera",
                f"'{name}' is running live. Stop it before removing the camera.",
            )
            return
        using = [r.name for r in self.config.recordings.values() if r.camera_name == name]
        if any(r in self.running for r in using):
            messagebox.showwarning(
                "Remove Camera",
                f"Stop the recording(s) using '{name}' before removing it.",
            )
            return
        if not messagebox.askyesno("Remove Camera", f"Remove camera '{name}'? This cannot be undone."):
            return
        try:
            self.config.remove_camera(name)
        except SystemExit as e:
            messagebox.showerror("Remove Camera", str(e))
            return
        if name in self.config.live_cameras:
            self.config.live_cameras.remove(name)
        self.config.save()
        self.refresh_lists()

    # ---- recording actions ----

    def on_add_recording(self) -> None:
        if not self.config.cameras:
            messagebox.showinfo("Add Recording", "Add a camera first, then add a recording for it.")
            return
        RecordingDialog(self.root, self.config, None, self.refresh_lists)

    def on_edit_recording(self) -> None:
        recording = self.selected_recording()
        if not recording:
            messagebox.showinfo("Edit Recording", "Select a recording first.")
            return
        RecordingDialog(self.root, self.config, recording, self.refresh_lists)

    def on_remove_recording(self) -> None:
        name = self._tree_selection(self.recording_tree)
        if not name:
            messagebox.showinfo("Remove Recording", "Select a recording first.")
            return
        if name in self.running:
            messagebox.showwarning("Remove Recording", f"Stop '{name}' before removing it.")
            return
        if not messagebox.askyesno("Remove Recording", f"Remove recording '{name}'? This cannot be undone."):
            return
        self.config.remove_recording(name)
        self.config.save()
        self.refresh_lists()

    # ---- capture control ----

    def on_start(self) -> None:
        recording = self.selected_recording()
        if not recording:
            messagebox.showinfo("Start Capture", "Select a recording first.")
            return
        if recording.name in self.running:
            return
        try:
            setup = self.config.resolved(recording.name)
        except SystemExit as e:
            messagebox.showerror("Start Capture", str(e))
            return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run_worker, args=(setup, stop_event), daemon=True
        )
        self.running[recording.name] = RunningCapture(thread, stop_event)
        thread.start()
        self.refresh_lists()

    def _run_worker(self, setup, stop_event: threading.Event) -> None:
        try:
            run_scheduled(setup, log=self.log, stop_event=stop_event)
        except SystemExit as e:
            self.log(f"'{setup.name}' stopped: {e}")
        except Exception as e:
            self.log(f"'{setup.name}' crashed: {e}")

    def on_stop(self) -> None:
        name = self._tree_selection(self.recording_tree)
        if not name or name not in self.running:
            messagebox.showinfo("Stop Capture", "That recording isn't running.")
            return
        self.log(f"Stopping '{name}'...")
        # run_scheduled() itself builds a video from this session's frames
        # once it actually finishes stopping -- watch the log for it.
        self.running[name].stop_event.set()

    # ---- build ----

    def on_build(self) -> None:
        recording = self.selected_recording()
        if not recording:
            messagebox.showinfo("Build Video", "Select a recording first.")
            return
        try:
            setup = self.config.resolved(recording.name)
        except SystemExit as e:
            messagebox.showerror("Build Video", str(e))
            return
        BuildDialog(self.root, setup, self.log)

    def on_open_folder(self) -> None:
        recording = self.selected_recording()
        if not recording:
            messagebox.showinfo("Open Folder", "Select a recording first.")
            return
        os.makedirs(recording.output_dir, exist_ok=True)
        os.startfile(recording.output_dir)  # Windows only, matches this project's target platform

    # ---- live timelapse ----

    def _live_running(self, name: str) -> bool:
        rc = self.live_workers.get(name)
        return rc is not None and rc.thread.is_alive()

    def _selected_live_camera(self, action: str) -> Optional[str]:
        name = self._tree_selection(self.camera_tree)
        if not name or name not in self.config.cameras:
            messagebox.showinfo("Live Timelapse", f"Select a camera to {action}.")
            return None
        return name

    def on_live_start(self) -> None:
        name = self._selected_live_camera("start")
        if name is None:
            return
        if self._live_running(name):
            messagebox.showinfo("Live Timelapse", f"'{name}' is already running.")
            return
        camera = self.config.cameras[name]
        if name not in self.config.live_cameras:
            self.config.live_cameras.append(name)
            self.config.save()

        def status_cb(chunks: int, seg_mb: float) -> None:
            # Worker thread -> whole-dict swap per camera; the status loop
            # only ever reads, so no lock is needed.
            self.live_state[name] = {
                "chunks": chunks, "mb": seg_mb, "updated": dt.datetime.now(),
            }

        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._live_worker_fn, args=(name, camera, stop_event, status_cb),
            daemon=True,
        )
        self.live_workers[name] = RunningCapture(thread, stop_event)
        self.live_state.pop(name, None)
        thread.start()
        self._update_live_status()

    def _live_worker_fn(self, name, camera, stop_event, status_cb) -> None:
        try:
            run_live(camera, stop_event, log=self.log, status=status_cb)
        except SystemExit as e:
            self.log(f"Live timelapse for '{name}' stopped: {e}")
        except Exception as e:
            self.log(f"Live timelapse for '{name}' crashed: {e}")

    def on_live_stop(self) -> None:
        name = self._selected_live_camera("stop")
        if name is None:
            return
        if not self._live_running(name):
            messagebox.showinfo("Live Timelapse", f"'{name}' isn't running.")
            return
        self.log(f"Stopping live timelapse for '{name}' (finishing the current chunk)...")
        self.live_workers[name].stop_event.set()
        if name in self.config.live_cameras:
            self.config.live_cameras.remove(name)
            self.config.save()

    def on_play_live(self, filename: str) -> None:
        name = self._selected_live_camera("play")
        if name is None:
            return
        path = live_dirs(name)[2] / filename
        if not path.exists():
            messagebox.showinfo(
                "Live Timelapse",
                f"No live video for '{name}' yet -- start it and wait for the first chunk.",
            )
            return
        os.startfile(str(path))  # Windows only, same convention as Latest Videos

    def on_watch_live_vlc(self) -> None:
        """Open the last-hour feed in VLC through the local stream server.

        The URL is looped, and every loop pass re-requests the file -- so
        each replay shows the newest hour. Watching via HTTP (instead of
        the file directly) also keeps VLC from holding a lock that would
        block the live refresh.
        """
        name = self._selected_live_camera("watch")
        if name is None:
            return
        if not (live_dirs(name)[2] / "last_hour.mp4").exists():
            messagebox.showinfo(
                "Live Timelapse",
                f"No live video for '{name}' yet -- start it and wait for the first chunk.",
            )
            return
        prev = self.vlc_procs.get(name)
        if prev is not None and prev.poll() is None:
            messagebox.showinfo(
                "Watch in VLC",
                f"VLC is already watching '{name}' -- check your open windows.",
            )
            return
        url = stream_url(name)
        # Prove the stream answers before handing it to VLC. A looping VLC
        # retries an unreachable URL with zero delay -- a storm of window
        # flashes and error popups -- so never launch it on a dead stream.
        # start_stream_server also revives the server if it ever died.
        start_stream_server(log=self.log)
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as e:
            messagebox.showerror(
                "Watch in VLC",
                f"The live stream isn't answering, so VLC wasn't launched.\n\n"
                f"{e}\n\nCheck the log panel for stream server messages, "
                f"then try again.",
            )
            return
        vlc = shutil.which("vlc")
        if vlc is None:
            for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
                candidate = os.path.join(base or "", "VideoLAN", "VLC", "vlc.exe")
                if base and os.path.isfile(candidate):
                    vlc = candidate
                    break
        if vlc is None:
            messagebox.showinfo(
                "Watch in VLC",
                "VLC wasn't found on this PC. In VLC choose Media > Open Network "
                f"Stream, paste this address, and turn on Loop:\n\n{url}",
            )
            return
        # --no-interact: no dialog popups from VLC; a playback problem
        # shows in its window instead of spamming the desktop.
        self.vlc_procs[name] = subprocess.Popen(
            [vlc, "--loop", "--no-interact", url], **no_console_kwargs())
        self.log(f"Watching '{name}' in VLC via {url} -- looping, so each pass "
                 f"plays the newest hour.")

    def on_open_live_folder(self) -> None:
        name = self._selected_live_camera("open")
        if name is None:
            return
        root = live_dirs(name)[2]
        if not root.is_dir():
            messagebox.showinfo(
                "Live Timelapse",
                f"'{name}' has no live folder yet -- start it once to create one.",
            )
            return
        os.startfile(str(root))

    def _update_live_status(self) -> None:
        """Refresh each camera's live cells in place, only where changed."""
        for iid in self.camera_tree.get_children():
            cells = self._live_cells(iid)
            for column, value in zip(
                ("status", "chunks", "segments", "updated"), cells
            ):
                if self.camera_tree.set(iid, column) != value:
                    self.camera_tree.set(iid, column, value)

    # ---- shutdown ----

    def on_close(self) -> None:
        if self._closing:
            return
        workers = list(self.running.values())
        workers += [rc for name, rc in self.live_workers.items()
                    if rc.thread.is_alive()]
        if workers:
            if not messagebox.askyesno(
                "Quit", f"{len(workers)} capture(s) still running. Stop them and quit?"
            ):
                return
            self._closing = True
            self.root.title("Reolink Timelapse - finishing video builds...")
            for rc in workers:
                rc.stop_event.set()
            # Each worker stops ffmpeg and then builds its session's video
            # (or converts the final live chunk), which can take minutes -- a
            # fixed join timeout here would kill that work mid-write (daemon
            # threads die with the app). Wait them out instead, pumping the
            # UI so log lines stay visible.
            while any(rc.thread.is_alive() for rc in workers):
                for rc in workers:
                    rc.thread.join(timeout=0.2)
                try:
                    self.root.update()
                except tk.TclError:
                    break
        self._closed = True
        self.root.destroy()


class CameraDialog(tk.Toplevel):
    def __init__(self, parent, config: Config, existing: Optional[Camera], on_saved):
        super().__init__(parent)
        self.config = config
        self.existing = existing
        self.on_saved = on_saved
        self.title("Edit Camera" if existing else "Add Camera")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        pad = {"padx": 6, "pady": 4}
        self._row = 0

        def add_row(label: str, widget) -> None:
            ttk.Label(self, text=label).grid(row=self._row, column=0, sticky="e", **pad)
            widget.grid(row=self._row, column=1, sticky="we", **pad)
            self._row += 1

        self.name_var = tk.StringVar(value=existing.name if existing else "")
        name_entry = ttk.Entry(self, textvariable=self.name_var)
        if existing:
            name_entry.configure(state="disabled")
        add_row("Camera name", name_entry)

        self.ip_var = tk.StringVar(value=existing.ip if existing else "")
        add_row("Camera IP", ttk.Entry(self, textvariable=self.ip_var))

        self.port_var = tk.StringVar(value=str(existing.port if existing else 554))
        add_row("RTSP port", ttk.Entry(self, textvariable=self.port_var))

        self.user_var = tk.StringVar(value=existing.user if existing else "")
        add_row("Username", ttk.Entry(self, textvariable=self.user_var))

        self.password_var = tk.StringVar(value="")
        pw_label = "Password (leave blank to keep current)" if existing else "Password"
        add_row(pw_label, ttk.Entry(self, textvariable=self.password_var, show="*"))

        self.channel_var = tk.StringVar(value=str(existing.channel if existing else 1))
        add_row("Channel", ttk.Entry(self, textvariable=self.channel_var))

        self.stream_var = tk.StringVar(
            value="sub" if (existing.substream if existing else True) else "main"
        )
        stream_frame = ttk.Frame(self)
        ttk.Radiobutton(stream_frame, text="Substream (recommended)",
                         variable=self.stream_var, value="sub").pack(anchor="w")
        ttk.Radiobutton(stream_frame, text="Main stream (full res)",
                         variable=self.stream_var, value="main").pack(anchor="w")
        add_row("Stream", stream_frame)

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=self._row, column=0, columnspan=2, pady=8)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left", padx=4)

    def _save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Invalid", "Camera name is required.")
            return
        if not self.existing and name in self.config.cameras:
            messagebox.showerror("Invalid", f"A camera named '{name}' already exists.")
            return

        ip = self.ip_var.get().strip()
        user = self.user_var.get().strip()
        if not ip or not user:
            messagebox.showerror("Invalid", "Camera IP and username are required.")
            return

        password = self.password_var.get()
        if not password:
            if self.existing:
                password = self.existing.password
            else:
                messagebox.showerror("Invalid", "Password is required.")
                return

        try:
            port = int(self.port_var.get())
            channel = int(self.channel_var.get())
        except ValueError:
            messagebox.showerror("Invalid", "Port and channel must be numbers.")
            return

        camera = Camera(
            name=name, ip=ip, port=port, user=user, password=password,
            channel=channel, substream=(self.stream_var.get() == "sub"),
        )
        self.config.put_camera(camera)
        self.config.save()
        self.on_saved()
        self.destroy()


class RecordingDialog(tk.Toplevel):
    def __init__(self, parent, config: Config, existing: Optional[Recording], on_saved):
        super().__init__(parent)
        self.config = config
        self.existing = existing
        self.on_saved = on_saved
        self.title("Edit Recording" if existing else "Add Recording")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        pad = {"padx": 6, "pady": 4}
        self._row = 0

        def add_row(label: str, widget) -> None:
            ttk.Label(self, text=label).grid(row=self._row, column=0, sticky="e", **pad)
            widget.grid(row=self._row, column=1, sticky="we", **pad)
            self._row += 1

        self.name_var = tk.StringVar(value=existing.name if existing else "")
        name_entry = ttk.Entry(self, textvariable=self.name_var)
        if existing:
            name_entry.configure(state="disabled")
        add_row("Recording name", name_entry)

        camera_names = sorted(self.config.cameras)
        default_camera = existing.camera_name if existing else (camera_names[0] if camera_names else "")
        self.camera_var = tk.StringVar(value=default_camera)
        camera_combo = ttk.Combobox(self, textvariable=self.camera_var, state="readonly", values=camera_names)
        add_row("Camera", camera_combo)

        self.fps_var = tk.StringVar(value=f"{existing.output_fps:g}" if existing else "30")
        add_row("Output video fps", ttk.Entry(self, textvariable=self.fps_var, width=8))

        self._recommended_interval: Optional[float] = None
        self.pacing_var = tk.StringVar(
            value="length" if (existing and existing.target_video_seconds) else "interval"
        )
        pacing_frame = ttk.Frame(self)
        ttk.Radiobutton(pacing_frame, text="Choose a video length (recommended rate is derived)",
                         variable=self.pacing_var,
                         value="length", command=self._toggle_pacing_fields).pack(anchor="w")
        ttk.Radiobutton(pacing_frame, text="Set the frame interval manually", variable=self.pacing_var,
                         value="interval", command=self._toggle_pacing_fields).pack(anchor="w")
        add_row("Set capture by", pacing_frame)

        self.target_var = tk.StringVar(
            value=str(existing.target_video_seconds) if (existing and existing.target_video_seconds) else "60"
        )
        target_frame = ttk.Frame(self)
        self.target_entry = ttk.Entry(target_frame, textvariable=self.target_var, width=8)
        self.target_entry.pack(side="left")
        self.use_recommended_btn = ttk.Button(target_frame, text="Use as fixed interval",
                                              command=self._use_recommended)
        self.use_recommended_btn.pack(side="left", padx=6)
        add_row("Target video length (seconds)", target_frame)

        self.recommend_var = tk.StringVar(value="")
        recommend_label = ttk.Label(self, textvariable=self.recommend_var)
        recommend_label.grid(row=self._row, column=0, columnspan=2, sticky="w", padx=6)
        self._row += 1

        self.interval_var = tk.StringVar(value=str(existing.interval if existing else 30))
        interval_container = ttk.Frame(self)
        self._interval_widgets = []
        interval_entry = ttk.Entry(interval_container, textvariable=self.interval_var, width=8)
        interval_entry.pack(side="left")
        self._interval_widgets.append(interval_entry)
        for preset_label, seconds in [("5s", 5), ("10s", 10), ("30s", 30), ("1min", 60), ("5min", 300)]:
            btn = ttk.Button(interval_container, text=preset_label, width=5,
                             command=lambda s=seconds: self.interval_var.set(str(s)))
            btn.pack(side="left", padx=2)
            self._interval_widgets.append(btn)
        add_row("Seconds between frames", interval_container)

        self.estimate_var = tk.StringVar(value="")
        estimate_label = ttk.Label(self, textvariable=self.estimate_var, foreground="gray")
        estimate_label.grid(row=self._row, column=0, columnspan=2, sticky="w", padx=6)
        self._row += 1

        sched = existing.schedule if existing else Schedule()
        self.schedule_mode_var = tk.StringVar(value=sched.mode)
        mode_frame = ttk.Frame(self)
        for label, value in [
            ("Daylight hours", "daylight"),
            ("Fixed daily time", "fixed_time"),
            ("Timer (run for a set length)", "duration"),
            ("Always", "always"),
        ]:
            ttk.Radiobutton(mode_frame, text=label, variable=self.schedule_mode_var,
                             value=value, command=self._toggle_schedule_fields).pack(anchor="w")
        add_row("Schedule", mode_frame)

        self.lat_var = tk.StringVar(value="" if sched.latitude is None else str(sched.latitude))
        self.lon_var = tk.StringVar(value="" if sched.longitude is None else str(sched.longitude))
        loc_frame = ttk.Frame(self)
        self.lat_entry = ttk.Entry(loc_frame, textvariable=self.lat_var, width=11)
        self.lat_entry.pack(side="left")
        self.lon_entry = ttk.Entry(loc_frame, textvariable=self.lon_var, width=11)
        self.lon_entry.pack(side="left", padx=(4, 4))
        self.map_btn = ttk.Button(loc_frame, text="Pick on map...", command=self._open_map)
        self.map_btn.pack(side="left")
        add_row("Latitude / Longitude", loc_frame)

        tz_frame = ttk.Frame(self)
        self.tz_var = tk.StringVar(value=sched.timezone or "")
        self.tz_combo = ttk.Combobox(tz_frame, textvariable=self.tz_var, width=26, values=_TIMEZONES)
        self.tz_combo.pack(side="left")
        self.tz_detect_btn = ttk.Button(tz_frame, text="Detect from PC", command=self._detect_timezone)
        self.tz_detect_btn.pack(side="left", padx=4)
        add_row("Timezone", tz_frame)

        self.pre_offset_var = tk.StringVar(value=str(sched.pre_offset_minutes))
        self.pre_entry = ttk.Entry(self, textvariable=self.pre_offset_var)
        add_row("Minutes before sunrise to start", self.pre_entry)

        self.post_offset_var = tk.StringVar(value=str(sched.post_offset_minutes))
        self.post_entry = ttk.Entry(self, textvariable=self.post_offset_var)
        add_row("Minutes after sunset to stop", self.post_entry)

        self.start_time_var = tk.StringVar(value=sched.start_time or "07:00")
        self.start_time_entry = ttk.Entry(self, textvariable=self.start_time_var)
        add_row("Start time (24h HH:MM)", self.start_time_entry)

        self.end_time_var = tk.StringVar(value=sched.end_time or "19:00")
        self.end_time_entry = ttk.Entry(self, textvariable=self.end_time_var)
        add_row("End time (24h HH:MM)", self.end_time_entry)

        self.duration_var = tk.StringVar(
            value=str(sched.duration_minutes) if sched.duration_minutes else "60"
        )
        self.duration_entry = ttk.Entry(self, textvariable=self.duration_var)
        add_row("Run for (minutes)", self.duration_entry)

        self._daylight_widgets = [self.lat_entry, self.lon_entry, self.map_btn, self.pre_entry, self.post_entry]
        self._tz_widgets = [self.tz_combo, self.tz_detect_btn]
        self._fixed_widgets = [self.start_time_entry, self.end_time_entry]
        self._duration_widgets = [self.duration_entry]

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=self._row, column=0, columnspan=2, pady=8)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left", padx=4)

        # Wire the live estimate after every var exists; anything that
        # changes the math re-renders it.
        for var in (self.interval_var, self.fps_var, self.target_var,
                    self.start_time_var, self.end_time_var, self.duration_var,
                    self.lat_var, self.lon_var, self.tz_var,
                    self.pre_offset_var, self.post_offset_var):
            var.trace_add("write", self._update_estimate)

        self._toggle_schedule_fields()
        self._toggle_pacing_fields()

    def _toggle_schedule_fields(self) -> None:
        mode = self.schedule_mode_var.get()
        daylight_state = "normal" if mode == "daylight" else "disabled"
        fixed_state = "normal" if mode == "fixed_time" else "disabled"
        tz_state = "normal" if mode in ("daylight", "fixed_time") else "disabled"
        duration_state = "normal" if mode == "duration" else "disabled"
        for w in self._daylight_widgets:
            w.configure(state=daylight_state)
        for w in self._fixed_widgets:
            w.configure(state=fixed_state)
        for w in self._tz_widgets:
            w.configure(state=tz_state)
        for w in self._duration_widgets:
            w.configure(state=duration_state)
        self._update_estimate()

    def _toggle_pacing_fields(self) -> None:
        length = self.pacing_var.get() == "length"
        for w in self._interval_widgets:
            w.configure(state="disabled" if length else "normal")
        self.target_entry.configure(state="normal" if length else "disabled")
        self.use_recommended_btn.configure(state="normal" if length else "disabled")
        self._update_estimate()

    def _use_recommended(self) -> None:
        """Lock the currently recommended rate: copy it into the interval
        field and switch to manual-interval pacing, so the recording stores
        a plain fixed interval instead of auto-adjusting each session."""
        self._update_estimate()
        if self._recommended_interval is None:
            messagebox.showinfo(
                "Use as fixed interval",
                "Enter a target video length first, with a schedule that has a "
                "set session length (Daylight hours, Fixed daily time, or Timer).",
            )
            return
        self.interval_var.set(f"{round(self._recommended_interval, 2):g}")
        self.pacing_var.set("interval")
        self._toggle_pacing_fields()

    def _session_seconds(self) -> Optional[float]:
        """Length of one capture session for the currently entered schedule,
        or None when it has no defined length (Always mode / incomplete
        fields). Best-effort -- feeds the estimate label only."""
        mode = self.schedule_mode_var.get()
        if mode == "duration":
            minutes = _parse_optional_int_or_none(self.duration_var.get())
            return minutes * 60 if minutes and minutes > 0 else None
        if mode == "fixed_time":
            start_s, end_s = self.start_time_var.get().strip(), self.end_time_var.get().strip()
            if not (_valid_hhmm(start_s) and _valid_hhmm(end_s)):
                return None
            delta = (dt.datetime.strptime(end_s, "%H:%M")
                     - dt.datetime.strptime(start_s, "%H:%M")).total_seconds()
            return delta if delta > 0 else delta + 86400  # overnight window
        if mode == "daylight":
            latitude = _parse_optional_float(self.lat_var.get())
            longitude = _parse_optional_float(self.lon_var.get())
            tz_name = self.tz_var.get().strip()
            if latitude is None or longitude is None or not is_valid_timezone(tz_name):
                return None
            try:
                temp = Setup(name="", ip="", user="", password="", schedule=Schedule(
                    mode="daylight", latitude=latitude, longitude=longitude, timezone=tz_name,
                    pre_offset_minutes=_parse_optional_int(self.pre_offset_var.get()),
                    post_offset_minutes=_parse_optional_int(self.post_offset_var.get()),
                ))
                window = daylight_window(temp, dt.date.today())
                return (window.end - window.start).total_seconds()
            except Exception:
                return None
        return None  # "always"

    def _update_estimate(self, *_args) -> None:
        self._recommended_interval = None
        try:
            fps = float(self.fps_var.get())
            if fps <= 0:
                raise ValueError
        except ValueError:
            self.recommend_var.set("")
            self.estimate_var.set("")
            return
        session_secs = self._session_seconds()

        if self.pacing_var.get() == "length":
            self.estimate_var.set("")
            target = _parse_optional_int_or_none(self.target_var.get())
            if not target or target <= 0:
                self.recommend_var.set("")
                return
            if session_secs is None:
                self.recommend_var.set(
                    "Recommended rate needs a schedule with a set session length (not Always)."
                )
                return
            interval = max(session_secs / (target * fps), 0.05)
            self._recommended_interval = interval
            self.recommend_var.set(
                f"Recommended: 1 frame every {interval:.2f}s "
                f"(~{target * fps:,.0f} frames over a {session_secs / 3600:.1f}h session "
                f"-> {target}s at {fps:g} fps)"
            )
            self.estimate_var.set(
                "Saving keeps auto-adjust: the rate is re-derived from each session's "
                "actual window. \"Use as fixed interval\" locks today's rate instead."
            )
            return

        self.recommend_var.set("")
        try:
            interval = float(self.interval_var.get())
            if interval <= 0:
                raise ValueError
        except ValueError:
            self.estimate_var.set("")
            return
        if session_secs is None:
            frames_per_hour = 3600 / interval
            self.estimate_var.set(
                f"~{frames_per_hour:.0f} frames/hour -> {frames_per_hour / fps:.1f}s of video "
                f"per hour of capture at {fps:g} fps"
            )
        else:
            frames = session_secs / interval
            self.estimate_var.set(
                f"~{frames:.0f} frames over a {session_secs / 3600:.1f}h session "
                f"-> ~{frames / fps:.1f}s video at {fps:g} fps"
            )

    def _open_map(self) -> None:
        LocationMapDialog(self, self.lat_var, self.lon_var)

    def _detect_timezone(self) -> None:
        try:
            name = tzlocal.get_localzone_name()
        except Exception as e:
            messagebox.showerror("Detect timezone", f"Couldn't detect the PC's timezone: {e}")
            return
        if not name:
            messagebox.showerror("Detect timezone", "Couldn't detect the PC's timezone.")
            return
        self.tz_var.set(name)

    def _save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Invalid", "Recording name is required.")
            return
        if not self.existing and name in self.config.recordings:
            messagebox.showerror("Invalid", f"A recording named '{name}' already exists.")
            return

        camera_name = self.camera_var.get().strip()
        if not camera_name or camera_name not in self.config.cameras:
            messagebox.showerror("Invalid", "Select a camera.")
            return

        try:
            output_fps = float(self.fps_var.get())
            if output_fps <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid", "Output video fps must be a number greater than 0.")
            return

        mode = self.schedule_mode_var.get()

        target_video_seconds = None
        if self.pacing_var.get() == "length":
            if mode == "always":
                messagebox.showerror(
                    "Invalid",
                    "Target video length needs a schedule with a set session length "
                    "(Daylight hours, Fixed daily time, or Timer) -- use interval "
                    "pacing for Always mode.",
                )
                return
            target_video_seconds = _parse_optional_int_or_none(self.target_var.get())
            if not target_video_seconds or target_video_seconds <= 0:
                messagebox.showerror("Invalid", "Enter the target video length in seconds.")
                return
            # interval is unused while length-paced but kept as a sane
            # fallback value; don't block save on whatever's in that box.
            interval = _parse_optional_float(self.interval_var.get())
            if not interval or interval <= 0:
                interval = self.existing.interval if self.existing else 30
        else:
            try:
                interval = float(self.interval_var.get())
                if interval <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid", "Seconds between frames must be a number greater than 0.")
                return
        # Parse every schedule field regardless of the active mode so
        # switching modes and saving never discards already-entered values --
        # switching back later (even in a future edit) won't require re-typing.
        latitude = _parse_optional_float(self.lat_var.get())
        longitude = _parse_optional_float(self.lon_var.get())
        timezone = self.tz_var.get().strip() or None
        pre_offset = _parse_optional_int(self.pre_offset_var.get())
        post_offset = _parse_optional_int(self.post_offset_var.get())
        start_time = self.start_time_var.get().strip() or None
        end_time = self.end_time_var.get().strip() or None
        duration_minutes = _parse_optional_int_or_none(self.duration_var.get())

        if mode in ("daylight", "fixed_time"):
            if not timezone or not is_valid_timezone(timezone):
                messagebox.showerror(
                    "Invalid timezone",
                    f"'{timezone or ''}' isn't a recognized IANA timezone name, "
                    f"e.g. America/New_York, Europe/London, Asia/Tokyo.",
                )
                return
        if mode == "daylight":
            if latitude is None or longitude is None:
                messagebox.showerror("Invalid", "Latitude and longitude are required for daylight-hours scheduling.")
                return
        elif mode == "fixed_time":
            if not start_time or not _valid_hhmm(start_time) or not end_time or not _valid_hhmm(end_time):
                messagebox.showerror("Invalid", "Start and end time must be in 24h HH:MM form, e.g. 07:00.")
                return
        elif mode == "duration":
            if not duration_minutes or duration_minutes <= 0:
                messagebox.showerror("Invalid", "Enter how many minutes to run for.")
                return

        schedule = Schedule(
            mode=mode, latitude=latitude, longitude=longitude, timezone=timezone,
            pre_offset_minutes=pre_offset, post_offset_minutes=post_offset,
            start_time=start_time, end_time=end_time, duration_minutes=duration_minutes,
        )

        recording = Recording(
            name=name, camera_name=camera_name, interval=interval,
            output_fps=output_fps, target_video_seconds=target_video_seconds,
            schedule=schedule,
        )
        self.config.put_recording(recording)
        self.config.save()
        self.on_saved()
        self.destroy()


# Simplified continental-US outline (lon, lat), hand-picked for a rough
# clickable reference shape -- not survey-accurate. The numeric lat/lon
# fields remain editable afterward for fine-tuning.
US_OUTLINE = [
    (-124.7, 48.4), (-124.1, 44.6), (-123.0, 39.5), (-122.5, 37.8), (-120.5, 34.5), (-117.2, 32.7),
    (-114.7, 32.7), (-111.0, 31.3), (-108.2, 31.3), (-106.5, 31.8), (-104.9, 29.5), (-99.5, 26.4), (-97.4, 25.9),
    (-97.2, 27.8), (-95.3, 28.9), (-93.8, 29.7), (-90.0, 29.1), (-89.0, 30.3), (-87.2, 30.4),
    (-85.0, 29.7), (-83.0, 29.6), (-82.6, 27.8), (-81.8, 25.2), (-80.1, 25.8),
    (-80.5, 28.5), (-81.4, 30.3), (-79.9, 32.8), (-77.9, 34.2), (-76.0, 36.9), (-75.5, 38.8),
    (-74.0, 40.6), (-71.0, 41.5), (-70.2, 43.7), (-67.0, 44.8),
    (-71.5, 45.0), (-79.0, 43.3), (-83.5, 45.8), (-84.5, 46.5), (-89.5, 48.0), (-95.2, 49.0),
    (-104.0, 49.0), (-114.0, 49.0), (-117.0, 49.0),
]

_MAP_LON_RANGE = (-125.0, -66.9)
_MAP_LAT_RANGE = (24.5, 49.5)
_MAP_SIZE = (560, 340)
_MAP_PAD = 10


class LocationMapDialog(tk.Toplevel):
    def __init__(self, parent, lat_var: tk.StringVar, lon_var: tk.StringVar):
        super().__init__(parent)
        self.lat_var = lat_var
        self.lon_var = lon_var
        self.title("Pick a location")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._pending: Optional[tuple] = None
        self._marker = None

        ttk.Label(
            self, text="Click roughly where the camera is -- you can fine-tune "
                       "the latitude/longitude afterward."
        ).pack(padx=8, pady=(8, 4))

        width, height = _MAP_SIZE
        self.canvas = tk.Canvas(
            self, width=width, height=height, background="#dbe9f4",
            highlightthickness=1, highlightbackground="#888888",
        )
        self.canvas.pack(padx=8, pady=4)
        self.canvas.bind("<Button-1>", self._on_click)

        points = []
        for lon, lat in US_OUTLINE:
            x, y = self._project(lon, lat)
            points.extend([x, y])
        self.canvas.create_polygon(points, fill="#f0dfb4", outline="#8b7355", width=2)

        self.coord_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.coord_var).pack(pady=(0, 4))

        lat = _parse_optional_float(lat_var.get())
        lon = _parse_optional_float(lon_var.get())
        if lat is not None and lon is not None:
            x, y = self._project(lon, lat)
            self._draw_marker(x, y)
            self.coord_var.set(f"{lat:.4f}, {lon:.4f}")
            self._pending = (lat, lon)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=(0, 8))
        ttk.Button(btn_frame, text="Use this location", command=self._confirm).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left", padx=4)

    def _project(self, lon: float, lat: float) -> tuple:
        lon_min, lon_max = _MAP_LON_RANGE
        lat_min, lat_max = _MAP_LAT_RANGE
        width, height = _MAP_SIZE
        x = _MAP_PAD + (lon - lon_min) / (lon_max - lon_min) * (width - 2 * _MAP_PAD)
        y = _MAP_PAD + (lat_max - lat) / (lat_max - lat_min) * (height - 2 * _MAP_PAD)
        return x, y

    def _unproject(self, x: float, y: float) -> tuple:
        lon_min, lon_max = _MAP_LON_RANGE
        lat_min, lat_max = _MAP_LAT_RANGE
        width, height = _MAP_SIZE
        lon = lon_min + (x - _MAP_PAD) / (width - 2 * _MAP_PAD) * (lon_max - lon_min)
        lat = lat_max - (y - _MAP_PAD) / (height - 2 * _MAP_PAD) * (lat_max - lat_min)
        return lat, lon

    def _draw_marker(self, x: float, y: float) -> None:
        if self._marker is not None:
            self.canvas.delete(self._marker)
        r = 5
        self._marker = self.canvas.create_oval(
            x - r, y - r, x + r, y + r, fill="#c0392b", outline="black"
        )

    def _on_click(self, event) -> None:
        lat, lon = self._unproject(event.x, event.y)
        lat = max(min(lat, 90.0), -90.0)
        lon = max(min(lon, 180.0), -180.0)
        self._draw_marker(event.x, event.y)
        self.coord_var.set(f"{lat:.4f}, {lon:.4f}")
        self._pending = (lat, lon)

    def _confirm(self) -> None:
        if self._pending is None:
            messagebox.showinfo("Pick a location", "Click a point on the map first.")
            return
        lat, lon = self._pending
        self.lat_var.set(f"{lat:.4f}")
        self.lon_var.set(f"{lon:.4f}")
        self.destroy()


ALL_SESSIONS = "All sessions (entire history)"


class BuildDialog(tk.Toplevel):
    def __init__(self, parent, setup, log):
        super().__init__(parent)
        self.setup = setup
        self.log = log
        self.title(f"Build video - {setup.name}")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        pad = {"padx": 6, "pady": 4}
        row = 0

        def add_row(label: str, widget) -> int:
            nonlocal row
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="e", **pad)
            widget.grid(row=row, column=1, sticky="we", **pad)
            row += 1
            return row

        # Sessions newest-first, defaulting to the most recent one. Building
        # "everything ever captured" when the user just wants the session
        # they recorded produced videos that opened with frames from long
        # before -- make session scope the explicit, default choice.
        self.sessions = self._list_sessions()
        scope_values = [label for label, _ in self.sessions] + [ALL_SESSIONS]
        self.scope_var = tk.StringVar(value=scope_values[0])
        self.scope_combo = ttk.Combobox(
            self, textvariable=self.scope_var, state="readonly",
            values=scope_values, width=34,
        )
        add_row("Session to build", self.scope_combo)
        self.scope_combo.bind("<<ComboboxSelected>>", lambda _e: self._toggle_smooth())

        self.fps_var = tk.StringVar(value=f"{setup.output_fps:g}")
        self.fps_entry = ttk.Entry(self, textvariable=self.fps_var)
        add_row("Output fps", self.fps_entry)

        self.smooth_var = tk.BooleanVar(value=False)
        self.base_fps_var = tk.StringVar(value="5")
        smooth_frame = ttk.Frame(self)
        self.smooth_check = ttk.Checkbutton(
            smooth_frame, text="Smooth motion (interpolate in-between frames)",
            variable=self.smooth_var, command=self._toggle_smooth,
        )
        self.smooth_check.pack(anchor="w")
        base_row = ttk.Frame(smooth_frame)
        base_row.pack(anchor="w")
        ttk.Label(base_row, text="Real frames per second:").pack(side="left")
        self.base_fps_entry = ttk.Entry(base_row, textvariable=self.base_fps_var, width=6)
        self.base_fps_entry.pack(side="left", padx=4)
        add_row("Smoothing", smooth_frame)

        self.est_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.est_var, foreground="gray").grid(
            row=row, column=0, columnspan=2, sticky="w", padx=6)
        row += 1
        self.fps_var.trace_add("write", self._update_build_estimate)
        self.base_fps_var.trace_add("write", self._update_build_estimate)

        self.date_var = tk.StringVar(value="")
        self.date_entry = ttk.Entry(self, textvariable=self.date_var)
        add_row("Single date (YYYY-MM-DD, optional)", self.date_entry)

        self.start_date_var = tk.StringVar(value="")
        self.start_date_entry = ttk.Entry(self, textvariable=self.start_date_var)
        add_row("Start date (optional)", self.start_date_entry)

        self.end_date_var = tk.StringVar(value="")
        self.end_date_entry = ttk.Entry(self, textvariable=self.end_date_var)
        add_row("End date (optional)", self.end_date_entry)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var).grid(row=row, column=0, columnspan=2, **pad)
        row += 1

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=8)
        self.build_btn = ttk.Button(btn_frame, text="Build", command=self._start_build)
        self.build_btn.pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Close", command=self.destroy).pack(side="left", padx=4)

        # After every widget exists -- this gates field states by scope.
        self._toggle_smooth()

    def _list_sessions(self) -> list:
        """[(label, path)] for buildable sessions, newest first.

        Two kinds coexist. Recordings made by the current version capture
        video and render segments as they go, so rebuilding is a lossless
        concat -- their pacing is already baked in. Sessions captured by
        older versions are loose JPEGs and still go through
        build_timelapse, which can re-time and smooth them. `_scope_kind`
        records which is which so the dialog can enable the right fields.
        """
        sessions = []
        self._scope_counts: dict = {}
        self._scope_kind: dict = {}

        # Newer, segment-based sessions first (they're the recent ones).
        seg_root = Path(self.setup.output_dir) / "sessions"
        if seg_root.is_dir():
            for d in sorted((p for p in seg_root.iterdir() if p.is_dir()), reverse=True):
                segs = sorted((d / "segments").glob("*_tl.mp4"))
                if not segs:
                    continue
                try:
                    started = dt.datetime.strptime(d.name[:15], "%Y%m%d_%H%M%S")
                    when = f"{started:%Y-%m-%d %H:%M}"
                except ValueError:
                    when = d.name
                label = f"Session {when} ({len(segs)} clips)"
                sessions.append((label, str(d)))
                self._scope_counts[label] = len(segs)
                self._scope_kind[label] = "segments"

        root = Path(self.setup.frames_dir)
        total = 0
        if root.is_dir():
            total = sum(1 for _ in root.glob("*.jpg"))  # legacy loose frames
            for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
                count = sum(1 for _ in d.glob("*.jpg"))
                if not count:
                    continue
                try:
                    started = dt.datetime.strptime(d.name[:15], "%Y%m%d_%H%M%S")
                    label = f"Session {started:%Y-%m-%d %H:%M} ({count} frames)"
                except ValueError:
                    label = f"Session {d.name} ({count} frames)"
                sessions.append((label, str(d)))
                self._scope_counts[label] = count
                self._scope_kind[label] = "frames"
                total += count
        self._scope_counts[ALL_SESSIONS] = total
        self._scope_kind[ALL_SESSIONS] = "frames"
        return sessions

    def _scope_is_segments(self) -> bool:
        return self._scope_kind.get(self.scope_var.get()) == "segments"

    def _toggle_smooth(self) -> None:
        """Enable only the fields that mean something for the current scope.

        For a segment-based session the capture interval and fps were fixed
        when it was recorded, and rebuilding just re-joins the rendered
        clips -- so offering fps, smoothing or date filters there would be
        a lie. Legacy JPEG sessions keep the full set.
        """
        segments = self._scope_is_segments()
        for widget in (self.fps_entry, self.smooth_check,
                       self.date_entry, self.start_date_entry, self.end_date_entry):
            widget.configure(state="disabled" if segments else "normal")
        smooth_on = self.smooth_var.get() and not segments
        self.base_fps_entry.configure(state="normal" if smooth_on else "disabled")
        self._update_build_estimate()

    def _update_build_estimate(self, *_args) -> None:
        frames = self._scope_counts.get(self.scope_var.get(), 0)
        if self._scope_is_segments():
            self.est_var.set(
                f"Joins {frames} recorded clip(s) as-is -- speed and fps were "
                f"set when this session was captured."
            )
            return
        try:
            fps = float(self.fps_var.get())
            if fps <= 0:
                raise ValueError
        except ValueError:
            self.est_var.set("")
            return
        if self.smooth_var.get():
            base = _parse_optional_float(self.base_fps_var.get())
            if not base or not 0 < base < fps:
                self.est_var.set("Real frames per second must be above 0 and below the output fps.")
                return
            self.est_var.set(
                f"~{frames / base:.1f}s of smoothed video from {frames} frames "
                f"({base:g} real fps interpolated to {fps:g} fps) -- renders much slower"
            )
            return
        self.est_var.set(f"~{frames / fps:.1f}s of video from {frames} frames at {fps:g} fps")

    def _parse_date(self, s: str) -> Optional[dt.date]:
        s = s.strip()
        return dt.date.fromisoformat(s) if s else None

    def _start_build(self) -> None:
        scope = self.scope_var.get()
        if self._scope_is_segments():
            session_dir = next((p for label, p in self.sessions if label == scope), None)
            self.build_btn.configure(state="disabled")
            self.status_var.set("Joining clips...")
            self.log(f"Rebuilding video for '{self.setup.name}' ({scope})...")
            self._result_queue = queue.Queue()
            threading.Thread(target=self._segment_worker,
                             args=(session_dir,), daemon=True).start()
            self.after(100, self._poll_result)
            return

        try:
            fps = float(self.fps_var.get())
            single_date = self._parse_date(self.date_var.get())
            start_date = single_date or self._parse_date(self.start_date_var.get())
            end_date = single_date or self._parse_date(self.end_date_var.get())
        except ValueError:
            messagebox.showerror("Invalid", "Check the fps/date fields (dates must be YYYY-MM-DD).")
            return

        smooth_base_fps = None
        if self.smooth_var.get():
            smooth_base_fps = _parse_optional_float(self.base_fps_var.get())
            if not smooth_base_fps or not 0 < smooth_base_fps < fps:
                messagebox.showerror(
                    "Invalid",
                    "Real frames per second must be greater than 0 and less than "
                    "the output fps.",
                )
                return

        frames_dir = next((path for label, path in self.sessions if label == scope), None)

        self.build_btn.configure(state="disabled")
        self.status_var.set("Building...")
        self.log(f"Building video for '{self.setup.name}' ({scope})"
                 + (" with motion smoothing..." if smooth_base_fps else "..."))

        self._result_queue: "queue.Queue[tuple]" = queue.Queue()
        threading.Thread(
            target=self._worker,
            args=(fps, start_date, end_date, frames_dir, smooth_base_fps), daemon=True,
        ).start()
        self.after(100, self._poll_result)

    def _worker(self, fps: float, start_date, end_date, frames_dir: Optional[str],
                smooth_base_fps: Optional[float]) -> None:
        # Runs on a background thread -- must never touch Tk widgets directly,
        # only hand the result to the main thread via the queue. Calling
        # self.after() from here intermittently raised "main thread is not
        # in main loop" (Tk requires after()/widget access from the thread
        # actually running mainloop), caught while testing the build flow.
        try:
            out = build_timelapse(
                self.setup, output_fps=fps, start_date=start_date, end_date=end_date,
                frames_dir=frames_dir, smooth_base_fps=smooth_base_fps,
            )
            self._result_queue.put((out, None))
        except (SystemExit, Exception) as e:
            self._result_queue.put((None, str(e)))

    def _segment_worker(self, session_dir: Optional[str]) -> None:
        """Rebuild a segment-based session: a lossless concat of its clips.

        Same background-thread rule as _worker -- hand results back through
        the queue, never touch Tk from here.
        """
        try:
            segments = sorted(Path(session_dir).joinpath("segments").glob("*_tl.mp4"))
            if not segments:
                raise RuntimeError("this session has no rendered clips left on disk.")
            stamp = Path(session_dir).name[:15]
            out = Path(self.setup.output_dir) / f"{self.setup.name}_{stamp}_rebuild.mp4"
            refresh_output(segments, out)
            self._result_queue.put((str(out), None))
        except (SystemExit, Exception) as e:
            self._result_queue.put((None, str(e)))

    def _poll_result(self) -> None:
        try:
            out, error = self._result_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_result)
            return
        self._done(out, error)

    def _done(self, out: Optional[str], error: Optional[str]) -> None:
        self.build_btn.configure(state="normal")
        if error:
            self.status_var.set("Failed.")
            self.log(f"Build failed: {error}")
            messagebox.showerror("Build failed", error)
        else:
            self.status_var.set("Done!")
            self.log(f"Build complete: {out}")
            messagebox.showinfo("Build complete", f"Saved to:\n{out}")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
