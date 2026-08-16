"""PyInstaller entry point for the windowed (no console) GUI build.

Separate from entrypoint.py so this one can be built with --windowed --
double-clicking it opens the control panel directly with no console window.
"""
from reolink_timelapse.gui import main

if __name__ == "__main__":
    main()
