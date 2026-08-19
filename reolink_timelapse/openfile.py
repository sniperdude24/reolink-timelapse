"""Open a file or folder with the OS's default handler, portably.

os.startfile (used everywhere in gui.py until now) is Windows-only --
AttributeError on Linux/macOS. This never raises: on a headless Pi
(SSH session, no desktop) there's nothing to hand the file to, so
callers get a clean False and can show the path instead of crashing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def open_path(path: str) -> bool:
    """Open `path` with its OS default handler. Returns whether it
    could -- never raises, so a missing desktop/tool is just a no-op."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
        return True
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
        return True
    # Linux: only meaningful with a desktop session actually attached --
    # a headless SSH session has no display for xdg-open to hand off to.
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    xdg_open = shutil.which("xdg-open")
    if not xdg_open:
        return False
    subprocess.Popen([xdg_open, path])
    return True
