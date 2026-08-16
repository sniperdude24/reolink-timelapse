"""Tkinter control panel: add/edit/remove setups, start/stop capture, build videos."""

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

from .build import build_timelapse
from .config import Config, Schedule, Setup, is_valid_timezone
from .scheduler import run_scheduled


@dataclass
class RunningCapture:
    thread: threading.Thread
    stop_event: threading.Event


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Reolink Timelapse")
        root.geometry("900x560")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.config = Config()
        self.running: dict[str, RunningCapture] = {}
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self._closed = False

        self._build_widgets()
        self.refresh_list()
        self.root.after(200, self._drain_log_queue)
        self.root.after(1000, self._refresh_status_loop)

    # ---- layout ----

    def _build_widgets(self) -> None:
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=8, pady=6)
        ttk.Button(toolbar, text="Add Setup", command=self.on_add).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Edit", command=self.on_edit).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Remove", command=self.on_remove).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="Start", command=self.on_start).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Stop", command=self.on_stop).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="Build Video...", command=self.on_build).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Open Folder", command=self.on_open_folder).pack(side="left", padx=2)

        columns = ("ip", "stream", "schedule", "status")
        self.tree = ttk.Treeview(self.root, columns=columns, show="tree headings", height=8)
        self.tree.heading("#0", text="Setup")
        self.tree.heading("ip", text="Camera")
        self.tree.heading("stream", text="Stream")
        self.tree.heading("schedule", text="Schedule")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=130)
        self.tree.column("ip", width=170)
        self.tree.column("stream", width=90)
        self.tree.column("schedule", width=110)
        self.tree.column("status", width=110)
        self.tree.pack(fill="x", padx=8, pady=(0, 6))

        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=16, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # ---- setup list ----

    def refresh_list(self) -> None:
        selected = self.selected_name()
        self.tree.delete(*self.tree.get_children())
        for name, s in sorted(self.config.setups.items()):
            status = "Running" if name in self.running else "Stopped"
            self.tree.insert("", "end", iid=name, text=name, values=(
                f"{s.ip}:{s.port}",
                "sub" if s.substream else "main",
                s.schedule.mode,
                status,
            ))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)

    def _refresh_status_loop(self) -> None:
        if self._closed:
            return
        for name in list(self.running):
            if not self.running[name].thread.is_alive():
                del self.running[name]
        self.refresh_list()
        self.root.after(1000, self._refresh_status_loop)

    def selected_name(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def selected_setup(self) -> Optional[Setup]:
        name = self.selected_name()
        return self.config.setups.get(name) if name else None

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

    # ---- setup actions ----

    def on_add(self) -> None:
        SetupDialog(self.root, self.config, None, self.refresh_list)

    def on_edit(self) -> None:
        setup = self.selected_setup()
        if not setup:
            messagebox.showinfo("Edit Setup", "Select a setup first.")
            return
        SetupDialog(self.root, self.config, setup, self.refresh_list)

    def on_remove(self) -> None:
        name = self.selected_name()
        if not name:
            messagebox.showinfo("Remove Setup", "Select a setup first.")
            return
        if name in self.running:
            messagebox.showwarning("Remove Setup", f"Stop '{name}' before removing it.")
            return
        if not messagebox.askyesno("Remove Setup", f"Remove setup '{name}'? This cannot be undone."):
            return
        self.config.remove(name)
        self.config.save()
        self.refresh_list()

    # ---- capture control ----

    def on_start(self) -> None:
        setup = self.selected_setup()
        if not setup:
            messagebox.showinfo("Start Capture", "Select a setup first.")
            return
        if setup.name in self.running:
            return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run_worker, args=(setup, stop_event), daemon=True
        )
        self.running[setup.name] = RunningCapture(thread, stop_event)
        thread.start()
        self.refresh_list()

    def _run_worker(self, setup: Setup, stop_event: threading.Event) -> None:
        try:
            run_scheduled(setup, log=self.log, stop_event=stop_event)
        except SystemExit as e:
            self.log(f"'{setup.name}' stopped: {e}")
        except Exception as e:
            self.log(f"'{setup.name}' crashed: {e}")

    def on_stop(self) -> None:
        name = self.selected_name()
        if not name or name not in self.running:
            messagebox.showinfo("Stop Capture", "That setup isn't running.")
            return
        self.log(f"Stopping '{name}'...")
        # run_scheduled() itself builds a video from this session's frames
        # once it actually finishes stopping -- watch the log for it.
        self.running[name].stop_event.set()

    # ---- build ----

    def on_build(self) -> None:
        setup = self.selected_setup()
        if not setup:
            messagebox.showinfo("Build Video", "Select a setup first.")
            return
        BuildDialog(self.root, setup, self.log)

    def on_open_folder(self) -> None:
        setup = self.selected_setup()
        if not setup:
            messagebox.showinfo("Open Folder", "Select a setup first.")
            return
        os.makedirs(setup.output_dir, exist_ok=True)
        os.startfile(setup.output_dir)  # Windows only, matches this project's target platform

    # ---- shutdown ----

    def on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(
                "Quit", f"{len(self.running)} setup(s) still capturing. Stop them and quit?"
            ):
                return
            for rc in self.running.values():
                rc.stop_event.set()
            for rc in self.running.values():
                rc.thread.join(timeout=15)
        self._closed = True
        self.root.destroy()


class SetupDialog(tk.Toplevel):
    def __init__(self, parent, config: Config, existing: Optional[Setup], on_saved):
        super().__init__(parent)
        self.config = config
        self.existing = existing
        self.on_saved = on_saved
        self.title("Edit Setup" if existing else "Add Setup")
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
        add_row("Setup name", name_entry)

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

        self.interval_var = tk.StringVar(value=str(existing.interval if existing else 30))
        add_row("Seconds between frames", ttk.Entry(self, textvariable=self.interval_var))

        default_base = Path.home() / "Timelapses" / (existing.name if existing else "setup")
        self.frames_dir_var = tk.StringVar(
            value=existing.frames_dir if existing else str(default_base / "frames")
        )
        frames_frame = ttk.Frame(self)
        ttk.Entry(frames_frame, textvariable=self.frames_dir_var, width=38).pack(side="left")
        ttk.Button(frames_frame, text="Browse...",
                   command=lambda: self._browse(self.frames_dir_var)).pack(side="left", padx=4)
        add_row("Frames folder", frames_frame)

        self.output_dir_var = tk.StringVar(
            value=existing.output_dir if existing else str(default_base)
        )
        output_frame = ttk.Frame(self)
        ttk.Entry(output_frame, textvariable=self.output_dir_var, width=38).pack(side="left")
        ttk.Button(output_frame, text="Browse...",
                   command=lambda: self._browse(self.output_dir_var)).pack(side="left", padx=4)
        add_row("Video output folder", output_frame)

        sched = existing.schedule if existing else Schedule()
        self.schedule_mode_var = tk.StringVar(value=sched.mode)
        mode_frame = ttk.Frame(self)
        ttk.Radiobutton(mode_frame, text="Daylight hours only", variable=self.schedule_mode_var,
                         value="daylight", command=self._toggle_daylight_fields).pack(anchor="w")
        ttk.Radiobutton(mode_frame, text="Always", variable=self.schedule_mode_var,
                         value="always", command=self._toggle_daylight_fields).pack(anchor="w")
        add_row("Schedule", mode_frame)

        self.lat_var = tk.StringVar(value="" if sched.latitude is None else str(sched.latitude))
        self.lat_entry = ttk.Entry(self, textvariable=self.lat_var)
        add_row("Latitude", self.lat_entry)

        self.lon_var = tk.StringVar(value="" if sched.longitude is None else str(sched.longitude))
        self.lon_entry = ttk.Entry(self, textvariable=self.lon_var)
        add_row("Longitude", self.lon_entry)

        self.tz_var = tk.StringVar(value=sched.timezone or "")
        self.tz_entry = ttk.Entry(self, textvariable=self.tz_var)
        add_row("Timezone (e.g. America/New_York)", self.tz_entry)

        self.pre_offset_var = tk.StringVar(value=str(sched.pre_offset_minutes))
        self.pre_entry = ttk.Entry(self, textvariable=self.pre_offset_var)
        add_row("Minutes before sunrise to start", self.pre_entry)

        self.post_offset_var = tk.StringVar(value=str(sched.post_offset_minutes))
        self.post_entry = ttk.Entry(self, textvariable=self.post_offset_var)
        add_row("Minutes after sunset to stop", self.post_entry)

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=self._row, column=0, columnspan=2, pady=8)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left", padx=4)

        self._toggle_daylight_fields()

    def _toggle_daylight_fields(self) -> None:
        state = "normal" if self.schedule_mode_var.get() == "daylight" else "disabled"
        for entry in (self.lat_entry, self.lon_entry, self.tz_entry, self.pre_entry, self.post_entry):
            entry.configure(state=state)

    def _browse(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if path:
            var.set(path)

    def _save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Invalid", "Setup name is required.")
            return
        if not self.existing and name in self.config.setups:
            messagebox.showerror("Invalid", f"A setup named '{name}' already exists.")
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
            interval = float(self.interval_var.get())
        except ValueError:
            messagebox.showerror("Invalid", "Port, channel, and interval must be numbers.")
            return

        frames_dir = self.frames_dir_var.get().strip() or str(Path.home() / "Timelapses" / name / "frames")
        output_dir = self.output_dir_var.get().strip() or str(Path.home() / "Timelapses" / name)

        mode = self.schedule_mode_var.get()
        if mode == "daylight":
            try:
                latitude = float(self.lat_var.get())
                longitude = float(self.lon_var.get())
                pre_offset = int(self.pre_offset_var.get() or 0)
                post_offset = int(self.post_offset_var.get() or 0)
            except ValueError:
                messagebox.showerror("Invalid", "Latitude, longitude, and offsets must be numbers.")
                return
            timezone = self.tz_var.get().strip()
            if not is_valid_timezone(timezone):
                messagebox.showerror(
                    "Invalid timezone",
                    f"'{timezone}' isn't a recognized IANA timezone name, "
                    f"e.g. America/New_York, Europe/London, Asia/Tokyo.",
                )
                return
            schedule = Schedule(
                mode="daylight", latitude=latitude, longitude=longitude,
                timezone=timezone, pre_offset_minutes=pre_offset, post_offset_minutes=post_offset,
            )
        else:
            schedule = Schedule(mode="always")

        setup = Setup(
            name=name, ip=ip, port=port, user=user, password=password,
            channel=channel, substream=(self.stream_var.get() == "sub"),
            interval=interval, frames_dir=frames_dir, output_dir=output_dir,
            schedule=schedule,
        )
        self.config.put(setup)
        self.config.save()
        self.on_saved()
        self.destroy()


class BuildDialog(tk.Toplevel):
    def __init__(self, parent, setup: Setup, log):
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
