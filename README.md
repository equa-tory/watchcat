# watchcat

Tiny self-hosted status dashboard. An auto-tiling grid of big buttons - click one to open the service, the dot shows whether it is up. Pure Python 3 standard library, no dependencies, one HTML file, a few KB over the wire.

## Install

**Linux / macOS**
```sh
./install.sh          # asks for an optional password, offers a systemd service (`/etc/systemd/system/watchcat.service`, uses sudo)
./install.sh --run    # just run in the foreground
```

**Windows**
```bat
install.bat
```

Needs Python 3.8+. Open `http://localhost:8888`.

## Use

Tap the faint gear in the bottom-right corner (it lights up on hover): add, edit, reorder and remove services, and manage backups.

- **Fits any screen**: tiles stretch to fill the whole screen with no empty strips; on a phone they shrink so *every* service is visible at once. Settings -> Display -> **Bigger + scroll** switches to larger tiles and a scrolling page (remembered per browser).
- **Icons**: the site's own favicon is fetched automatically and cached in `data/icons`. Type an emoji/letters in *Icon* to override it.
- **Groups**: give services a group number. Same number = same color and placed side by side; **lower numbers come first** (1, 2, 3...), group `0` (default) comes last. Colors are fixed per number (golden-angle hues, so they never change between reloads); under *Groups* you can give a group a name and pick its color.
- **Several addresses per tile** (e.g. local + remote): use **+ address**. The tile shows one clickable sub-block per address, each with its own status; the tile dot is green when all are up, amber when some are down.
- **Port-only services** (no web page, e.g. a game server or SSH): switch an address from *web* to *port only* and enter `host:port`. It is checked with a TCP connect; clicking it copies `host:port`.
- **Outage graph**: each tile shows a thin timeline (green = up, red = down, amber = some addresses down, empty = watchcat wasn't running) plus a line such as `last down 10/03 14:20 · 12m` or `down since 10/04 09:15`. Hover the graph for the last 6 outages. Pick 24 h / 7 days / 30 days (or Off) under Settings -> Display. It only appears on tiles big enough to hold it, so "fit all" on a phone with many services stays clean. A single failed check is not counted as an outage (it must fail twice in a row, and the outage is dated from the first failure). History is kept for 31 days in `data/history.json`; it stores state *changes* only, so it stays tiny and costs nothing noticeable.
- A web URL without a scheme is treated as `http://`. A web address is **up** when it answers with any HTTP status below 500 (login pages and 401/403 count as up); self-signed HTTPS certificates are accepted and environment proxies are ignored.

## Password (optional)

Create `.env` (see `.env.example`):

```
PASSWORD=your-secret
```

- `PASSWORD` set: everyone can **view** the dashboard, but changing services or touching backups requires logging in (the login form appears in the settings panel).
- No `.env` or empty `PASSWORD`: no login anywhere.

Other options: `PORT` (8888), `HOST` (0.0.0.0), `CHECK_INTERVAL` seconds (30), `DATA_DIR` (`./data`). Real environment variables override `.env`.

## Backups

Settings panel -> Backups. Defaults: folder `/mnt/ssd/backups/watchcat`, every **48 h**, keep **1**. Files are `watchcat-YYYYmmdd-HHMMSS.json`.

- **Backup now**, or **Download** the current list as a file.
- **Restore**: pick one of the backups found in the folder, or upload a file.
- Empty configurations are never backed up, so a wipe can't replace your only good backup.
- If the folder is unavailable (unmounted disk, permissions) the panel shows the error; nothing crashes.

Backups include services and group names/colors (icons are re-fetched; outage history is not included). Restoring replaces the service list and groups; backup settings are kept.

## Behind a reverse proxy

Works as-is. When serving over HTTPS, forward `X-Forwarded-Proto: https` so the session cookie gets the `Secure` flag.

## Files

`server.py` (HTTP API, checker, backups) - `static/index.html` (whole UI) - `data/` (your services, git-ignored).
