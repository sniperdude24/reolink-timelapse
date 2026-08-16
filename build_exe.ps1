# Builds a standalone reolink-timelapse.exe with ffmpeg bundled alongside it,
# so the resulting dist\reolink-timelapse\ folder needs nothing else installed.
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

Write-Host ""
Write-Host "Built: dist\reolink-timelapse\reolink-timelapse.exe"
Write-Host "ffmpeg is bundled alongside it -- the whole dist\reolink-timelapse folder is the distributable."
