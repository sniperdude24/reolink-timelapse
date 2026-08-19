#!/usr/bin/env bash
# Reverses install.sh's systemd registration. Your config and captured
# videos are left alone unless you pass --purge.
#
# Usage: ./packaging/pi/uninstall.sh [--purge]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

echo "== Stopping and disabling services =="
# Template units (@name) can have any number of running instances; list
# what's actually active/enabled rather than guessing camera/recording
# names, so this cleans up regardless of what was ever started.
mapfile -t UNITS < <(systemctl --user list-unit-files 'reolink-timelapse-*' \
  --no-legend 2>/dev/null | awk '{print $1}')
for unit in "${UNITS[@]}"; do
  systemctl --user disable --now "$unit" 2>/dev/null || true
done

echo "== Removing unit files =="
rm -f "$UNIT_DIR"/reolink-timelapse-*.service
systemctl --user daemon-reload

if [ "$PURGE" = "1" ]; then
  echo "== Purging venv, config, and captured media =="
  read -r -p "This deletes $REPO_DIR/.venv, config.yaml, and Timelapses/ -- type 'yes' to confirm: " confirm
  if [ "$confirm" = "yes" ]; then
    rm -rf "$REPO_DIR/.venv" "$REPO_DIR/config.yaml" "$REPO_DIR/Timelapses"
    echo "Purged."
  else
    echo "Skipped -- confirmation didn't match."
  fi
else
  echo "Services removed. Config and captured media left in place (rerun with --purge to also remove them)."
fi
