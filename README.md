# watchcat

Tiny self-hosted status dashboard. An auto-tiling grid of big buttons - click one to open the service, the dot shows whether it is up. Pure Python 3 standard library, no dependencies, one HTML file, ~6 KB over the wire.

## Install

**Linux / macOS**
```sh
./install.sh          # asks for an optional password, offers a systemd user service
./install.sh --run    # just run in the foreground
```

**Windows**
```bat
install.bat
```

Needs Python 3.8+. Open `http://localhost:8080`.

## Use

Hover the bottom-right corner and click the gear: add, edit, reorder and remove services, and manage backups. A URL without a scheme is treated as `http://`. A service is **up** when it answers with any HTTP status below 500 (login pages and 401/403 count as up); self-signed HTTPS certificates are accepted.

## Password (optional)

Create `.env` (see `.env.example`):

```
PASSWORD=your-secret
```

- `PASSWORD` set: everyone can **view** the dashboard, but changing services or touching backups requires logging in (the login form appears in the settings panel).
- No `.env` or empty `PASSWORD`: no login anywhere.

Other options: `PORT` (8080), `HOST` (0.0.0.0), `CHECK_INTERVAL` seconds (30), `DATA_DIR` (`./data`). Real environment variables override `.env`.

## Backups

Settings panel -> Backups. Defaults: folder `/mnt/ssd/backups/watchcat`, every **48 h**, keep **1**. Files are `watchcat-YYYYmmdd-HHMMSS.json`.

- **Backup now**, or **Download** the current list as a file.
- **Restore**: pick one of the backups found in the folder, or upload a file.
- Empty configurations are never backed up, so a wipe can't replace your only good backup.
- If the folder is unavailable (unmounted disk, permissions) the panel shows the error; nothing crashes.

Restoring replaces the service list; backup settings are kept.

## Behind a reverse proxy

Works as-is. When serving over HTTPS, forward `X-Forwarded-Proto: https` so the session cookie gets the `Secure` flag.

## Files

`server.py` (HTTP API, checker, backups) - `static/index.html` (whole UI) - `data/` (your services, git-ignored).
