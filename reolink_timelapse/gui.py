"""Tkinter control panel: add/edit/remove cameras and recordings, start/stop
capture, build videos."""

from __future__ import annotations

import datetime as dt
import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional
from zoneinfo import available_timezones

import tzlocal

from .build import build_timelapse
from .config import Camera, Config, Recording, Schedule, default_setup_dirs, is_valid_timezone
from .scheduler import next_window, run_scheduled

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
        root.geometry("900x620")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.config = Config()
        self.running: dict[str, RunningCapture] = {}
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self._closed = False

        self._build_widgets()
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
        cameras_frame = ttk.LabelFrame(self.root, text="Added Cameras")
        cameras_frame.pack(fill="x", padx=8, pady=(8, 4))

        cameras_toolbar = ttk.Frame(cameras_frame)
        cameras_toolbar.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Button(cameras_toolbar, text="Add Camera", command=self.on_add_camera).pack(side="left", padx=2)
        ttk.Button(cameras_toolbar, text="Remove", command=self.on_remove_camera).pack(side="left", padx=2)

        cameras_toolbar2 = ttk.Frame(cameras_frame)
        cameras_toolbar2.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(cameras_toolbar2, text="Edit", command=self.on_edit_camera).pack(side="left", padx=2)

        self.camera_tree = ttk.Treeview(
            cameras_frame, columns=("camera", "channel", "stream"), show="tree headings", height=5
        )
        self.camera_tree.heading("#0", text="Camera")
        self.camera_tree.heading("camera", text="Address")
        self.camera_tree.heading("channel", text="Channel")
        self.camera_tree.heading("stream", text="Stream")
        self.camera_tree.column("#0", width=130)
        self.camera_tree.column("camera", width=170)
        self.camera_tree.column("channel", width=80)
        self.camera_tree.column("stream", width=90)
        self.camera_tree.pack(fill="x", padx=4, pady=(0, 4))

        recordings_frame = ttk.LabelFrame(self.root, text="Scheduled Recordings")
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

        log_frame = ttk.LabelFrame(self.root, text="Log")
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

    def _refresh_camera_tree(self) -> None:
        selected = self._tree_selection(self.camera_tree)
        self.camera_tree.delete(*self.camera_tree.get_children())
        for name, c in sorted(self.config.cameras.items()):
            self.camera_tree.insert("", "end", iid=name, text=name, values=(
                f"{c.ip}:{c.port}",
                c.channel,
                "sub" if c.substream else "main",
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
        if self._closed:
            return
        for name in list(self.running):
            if not self.running[name].thread.is_alive():
                del self.running[name]
        self.refresh_lists()
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
        self.log_queue.put(msg)

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

    # ---- shutdown ----

    def on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(
                "Quit", f"{len(self.running)} recording(s) still capturing. Stop them and quit?"
            ):
                return
            for rc in self.running.values():
                rc.stop_event.set()
            for rc in self.running.values():
                rc.thread.join(timeout=15)
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

        self.interval_var = tk.StringVar(value=str(existing.interval if existing else 30))
        self.interval_var.trace_add("write", self._update_interval_estimate)

        interval_container = ttk.Frame(self)
        entry_row = ttk.Frame(interval_container)
        ttk.Entry(entry_row, textvariable=self.interval_var, width=8).pack(side="left")
        for preset_label, seconds in [("5s", 5), ("10s", 10), ("30s", 30), ("1min", 60), ("5min", 300)]:
            ttk.Button(entry_row, text=preset_label, width=5,
                       command=lambda s=seconds: self.interval_var.set(str(s))).pack(side="left", padx=2)
        entry_row.pack(anchor="w")

        self.interval_estimate_var = tk.StringVar(value="")
        ttk.Label(interval_container, textvariable=self.interval_estimate_var,
                  foreground="gray").pack(anchor="w", pady=(2, 0))
        add_row("Seconds between frames", interval_container)

        default_frames, default_output = default_setup_dirs(existing.name if existing else "recording")
        self.frames_dir_var = tk.StringVar(
            value=existing.frames_dir if existing else str(default_frames)
        )
        frames_frame = ttk.Frame(self)
        ttk.Entry(frames_frame, textvariable=self.frames_dir_var, width=38).pack(side="left")
        ttk.Button(frames_frame, text="Browse...",
                   command=lambda: self._browse(self.frames_dir_var)).pack(side="left", padx=4)
        add_row("Frames folder", frames_frame)

        self.output_dir_var = tk.StringVar(
            value=existing.output_dir if existing else str(default_output)
        )
        output_frame = ttk.Frame(self)
        ttk.Entry(output_frame, textvariable=self.output_dir_var, width=38).pack(side="left")
        ttk.Button(output_frame, text="Browse...",
                   command=lambda: self._browse(self.output_dir_var)).pack(side="left", padx=4)
        add_row("Video output folder", output_frame)

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

        self._toggle_schedule_fields()
        self._update_interval_estimate()

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

    def _update_interval_estimate(self, *_args) -> None:
        try:
            interval = float(self.interval_var.get())
            if interval <= 0:
                raise ValueError
        except ValueError:
            self.interval_estimate_var.set("")
            return
        frames_per_hour = 3600 / interval
        video_seconds_per_hour = frames_per_hour / 30
        self.interval_estimate_var.set(
            f"~{frames_per_hour:.0f} frames/hour -> {video_seconds_per_hour:.1f}s of video "
            f"per hour of capture at 30fps"
        )

    def _browse(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if path:
            var.set(path)

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
            interval = float(self.interval_var.get())
        except ValueError:
            messagebox.showerror("Invalid", "Seconds between frames must be a number.")
            return

        default_frames, default_output = default_setup_dirs(name)
        frames_dir = self.frames_dir_var.get().strip() or str(default_frames)
        output_dir = self.output_dir_var.get().strip() or str(default_output)

        mode = self.schedule_mode_var.get()
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
            frames_dir=frames_dir, output_dir=output_dir, schedule=schedule,
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

        self.fps_var = tk.StringVar(value="30")
        add_row("Output fps", ttk.Entry(self, textvariable=self.fps_var))

        self.date_var = tk.StringVar(value="")
        add_row("Single date (YYYY-MM-DD, optional)", ttk.Entry(self, textvariable=self.date_var))

        self.start_date_var = tk.StringVar(value="")
        add_row("Start date (optional)", ttk.Entry(self, textvariable=self.start_date_var))

        self.end_date_var = tk.StringVar(value="")
        add_row("End date (optional)", ttk.Entry(self, textvariable=self.end_date_var))

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var).grid(row=row, column=0, columnspan=2, **pad)
        row += 1

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=8)
        self.build_btn = ttk.Button(btn_frame, text="Build", command=self._start_build)
        self.build_btn.pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Close", command=self.destroy).pack(side="left", padx=4)

    def _parse_date(self, s: str) -> Optional[dt.date]:
        s = s.strip()
        return dt.date.fromisoformat(s) if s else None

    def _start_build(self) -> None:
        try:
            fps = float(self.fps_var.get())
            single_date = self._parse_date(self.date_var.get())
            start_date = single_date or self._parse_date(self.start_date_var.get())
            end_date = single_date or self._parse_date(self.end_date_var.get())
        except ValueError:
            messagebox.showerror("Invalid", "Check the fps/date fields (dates must be YYYY-MM-DD).")
            return

        self.build_btn.configure(state="disabled")
        self.status_var.set("Building...")
        self.log(f"Building video for '{self.setup.name}'...")

        self._result_queue: "queue.Queue[tuple]" = queue.Queue()
        threading.Thread(
            target=self._worker, args=(fps, start_date, end_date), daemon=True
        ).start()
        self.after(100, self._poll_result)

    def _worker(self, fps: float, start_date, end_date) -> None:
        # Runs on a background thread -- must never touch Tk widgets directly,
        # only hand the result to the main thread via the queue. Calling
        # self.after() from here intermittently raised "main thread is not
        # in main loop" (Tk requires after()/widget access from the thread
        # actually running mainloop), caught while testing the build flow.
        try:
            out = build_timelapse(
                self.setup, output_fps=fps, start_date=start_date, end_date=end_date,
            )
            self._result_queue.put((out, None))
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
