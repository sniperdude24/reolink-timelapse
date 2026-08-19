#!/usr/bin/env bash
# Linux/Raspberry Pi install: the "download and go" equivalent of the
# Windows release zip, but Linux-native -- ffmpeg comes from apt (Raspberry
# Pi OS's build already has V4L2 M2M hardware-decoder support and is
# GPL-compliant), and "leave it running" means a systemd --user service
# instead of a terminal window.
#
# Usage (run from inside the cloned repo):
#   ./packaging/pi/install.sh [--desktop]
#
# --desktop installs the Tkinter GUI's system dependencies too (python3-tk,
# xdg-utils) and builds the venv with --system-site-packages, which is
# required because apt's python3-tk lives in the system Python, not an
# isolated venv. Omit it for a headless (SSH/CLI-only) install -- the CLI
# never imports tkinter (cmd_gui only imports .gui inside the function
# body), so a headless box never needs it.
#
# This script only installs things and writes config for systemd -- it
# does not enable or start any service, and does not touch your camera
# config. Those are separate, printed steps at the end, so nothing runs
# unattended before you've had a chance to configure a camera.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

DESKTOP=0
for arg in "$@"; do
  case "$arg" in
    --desktop) DESKTOP=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

if ! command -v apt-get >/dev/null 2>&1; then
  echo "ERROR: this installer needs apt (Debian/Ubuntu/Raspberry Pi OS)." >&2
  echo "On another distro, install ffmpeg + Python 3 yourself, then:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

echo "== Installing system packages (sudo may prompt) =="
APT_PACKAGES=(ffmpeg python3-venv python3-pip)
if [ "$DESKTOP" = "1" ]; then
  APT_PACKAGES+=(python3-tk xdg-utils)
fi
sudo apt-get update
sudo apt-get install -y "${APT_PACKAGES[@]}"

echo "== Creating virtual environment =="
cd "$REPO_DIR"
if [ ! -d .venv ]; then
  if [ "$DESKTOP" = "1" ]; then
    # --system-site-packages: apt's python3-tk installs into the SYSTEM
    # Python, not into an isolated venv -- without this flag, the GUI
    # would fail with "No module named tkinter" inside the venv even
    # though python3-tk is installed.
    python3 -m venv --system-site-packages .venv
  else
    python3 -m venv .venv
  fi
else
  echo ".venv already exists, reusing it."
fi

echo "== Installing reolink-timelapse (editable) =="
# Editable, not a normal install: config.py's app_root_dir() resolves to
# two directories up from itself when not frozen, so it must keep living
# inside this checkout -- a copy-into-site-packages install would silently
# relocate config.yaml/Timelapses/ into the venv instead of this folder.
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

echo "== Installing systemd --user units =="
mkdir -p "$UNIT_DIR"
VENV_BIN="$REPO_DIR/.venv/bin"
for unit in "$SCRIPT_DIR"/systemd/*.service; do
  name="$(basename "$unit")"
  sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__VENV_BIN__|$VENV_BIN|g" \
    "$unit" > "$UNIT_DIR/$name"
done
systemctl --user daemon-reload

cat <<EOF

== Installed ==

Next steps (nothing above started or enabled anything):

1. Let your user's services keep running after you log out / disconnect SSH:
     loginctl enable-linger \$USER

2. Add a camera (interactive prompts):
     $VENV_BIN/reolink-timelapse configure <name>

3. Start the live-view web server (used by Watch-in-VLC-style URLs):
     systemctl --user enable --now reolink-timelapse-stream.service

4. Start a live rolling timelapse for a camera:
     systemctl --user enable --now reolink-timelapse-live@<camera-name>.service

   ...or a scheduled recording (configure it first with 'record'):
     systemctl --user enable --now reolink-timelapse-run@<recording-name>.service

Check status / logs:
     systemctl --user status reolink-timelapse-live@<camera-name>.service
     journalctl --user -u reolink-timelapse-live@<camera-name>.service -f

The live-view web server listens on your LAN by default on this platform
(no password) -- fine on a trusted home network, never port-forward it.
EOF
