# Builds two standalone builds, each with ffmpeg bundled alongside it so
# neither needs anything installed on the host:
#   dist\reolink-timelapse\      -- console CLI (configure/list/run/build/gui)
#   dist\reolink-timelapse-gui\  -- windowed control panel, no console window
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

pyinstaller --noconfirm --onedir --console --name reolink-timelapse `
    --collect-data tzdata `
    entrypoint.py
Copy-Item $ffmpeg.Source -Destination "dist\reolink-timelapse\ffmpeg.exe"

pyinstaller --noconfirm --onedir --windowed --name reolink-timelapse-gui `
    --collect-data tzdata `
    entrypoint_gui.py
Copy-Item $ffmpeg.Source -Destination "dist\reolink-timelapse-gui\ffmpeg.exe"

Write-Host ""
Write-Host "Built: dist\reolink-timelapse\reolink-timelapse.exe (command line)"
Write-Host "Built: dist\reolink-timelapse-gui\reolink-timelapse-gui.exe (double-click control panel)"
Write-Host "ffmpeg is bundled in both -- each dist folder is a complete, standalone distributable."
