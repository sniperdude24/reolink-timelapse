# Raspberry Pi / Linux packaging

See the main [README](../../README.md#raspberry-pi)'s Raspberry Pi section
for the full walkthrough. Quick reference for what lives here:

- `install.sh` -- installs ffmpeg + a venv + this package (editable) +
  systemd `--user` unit files. Run from inside a clone of this repo:
  `./packaging/pi/install.sh` (add `--desktop` for a GUI install).
- `uninstall.sh` -- removes the systemd units; `--purge` also removes the
  venv, config, and captured media.
- `systemd/*.service` -- the raw unit templates `install.sh` copies into
  `~/.config/systemd/user/`, substituting `__REPO_DIR__`/`__VENV_BIN__`
  for your actual install path. Read these directly if you'd rather set
  things up by hand than run a script.

Design notes (why it looks like this, not a Windows-style bundle):

- ffmpeg comes from `apt install ffmpeg`, not a bundled binary -- Raspberry
  Pi OS's build already includes V4L2 M2M hardware-decoder support and
  keeping it apt-managed avoids bundling a GPL binary per-architecture.
- The install is editable (`pip install -e .`) and stays inside the
  cloned repo on purpose: this project derives all its storage (config,
  captured video) from its own install folder, and a normal `pip install`
  would silently relocate that into site-packages.
- Units are `systemd --user`, not system-level -- no root/service account
  needed, and it keeps the config file's owner-only permissions
  (`chmod 0600`, set automatically on Linux) meaningful.
