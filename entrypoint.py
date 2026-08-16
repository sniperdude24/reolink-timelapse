"""PyInstaller entry point.

Must live outside the reolink_timelapse package and import it absolutely --
pointing PyInstaller directly at reolink_timelapse/__main__.py makes it treat
that file as a standalone top-level script, breaking its relative imports.
"""
from reolink_timelapse.cli import main

if __name__ == "__main__":
    main()
