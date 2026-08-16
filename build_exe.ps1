# Builds ONE standalone folder containing both the console CLI and the
# windowed GUI, sharing a single ffmpeg.exe -- and, by default, a single
# config.yaml and Timelapses/ folder (see app_root_dir() in config.py).
# Copy dist\reolink-timelapse\ anywhere and the whole install goes with it.
#   dist\reolink-timelapse\reolink-timelapse.exe      -- console CLI
#   dist\reolink-timelapse\reolink-timelapse-gui.exe  -- windowed control panel
#   dist\reolink-timelapse\ffmpeg.exe                 -- shared by both
#   dist\reolink-timelapse\_internal\                 -- CLI's bundled runtime
#   dist\reolink-timelapse\_internal_gui\             -- GUI's bundled runtime
# Usage: .\build_exe.ps1

$ErrorActionPreference = "Stop"

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Error "ffmpeg not found on PATH. Install it first (e.g. 'winget install Gyan.FFmpeg') and open a new terminal."
    exit 1
}
Write-Host "Bundling ffmpeg from: $($ffmpeg.Source)"

pip install --quiet --upgrade pyinstaller
pip install --quiet -r requirements.txt

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

# Console CLI -- this folder becomes the final shared distributable.
pyinstaller --noconfirm --onedir --console --name reolink-timelapse `
    --collect-data tzdata `
    entrypoint.py
Copy-Item $ffmpeg.Source -Destination "dist\reolink-timelapse\ffmpeg.exe"

# Windowed GUI -- built to a temp dist dir, then merged into the CLI's
# folder. --contents-directory renames its library folder to _internal_gui
# so it can't collide with the CLI's own _internal\ once merged.
pyinstaller --noconfirm --onedir --windowed --name reolink-timelapse-gui `
    --collect-data tzdata `
    --contents-directory _internal_gui `
    --distpath dist\_gui-temp `
    entrypoint_gui.py
Copy-Item "dist\_gui-temp\reolink-timelapse-gui\reolink-timelapse-gui.exe" -Destination "dist\reolink-timelapse\"
Copy-Item "dist\_gui-temp\reolink-timelapse-gui\_internal_gui" -Destination "dist\reolink-timelapse\_internal_gui" -Recurse
Remove-Item -Recurse -Force "dist\_gui-temp"

Write-Host ""
Write-Host "Built one folder: dist\reolink-timelapse\"
Write-Host "  reolink-timelapse.exe      (command line)"
Write-Host "  reolink-timelapse-gui.exe  (double-click control panel)"
Write-Host "ffmpeg is bundled once and shared by both. Config, frames, and videos also"
Write-Host "live inside this folder by default -- zip it up and it runs anywhere."
