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

## Server page (swipe right)

A second page with live stats about the machine watchcat runs on, built from the same tiles and the same *Fit all / Bigger + scroll* switch.

- **Phone:** swipe **left** (the server page is to the right of the services). The page follows your finger and snaps on release (drag past ~30 % or flick). Swipe right to come back. Vertical scrolling still works in *Bigger + scroll* mode.
- **PC:** move the mouse to the top of the screen and click the **Services | Server** pill that slides in; or press **→** (and **←** to return); or use a trackpad two-finger swipe; or click the faint tab on the right screen edge (left edge to return) or the dots at the bottom; or open **/#server** directly; or *Settings -> Server page -> Open server page*.
- **Tiles:** GPU temperature / load / power, VRAM, RAM (+swap), CPU load, **Ollama** (loaded models + all available), every **drive** (used / left; a dead network mount shows "not responding" instead of freezing the page), **Docker** containers, top **CPU** and **memory** processes, **frp** (service state, tunnel latency, each forwarded port with local→remote and up/down) and **xray** (ping through the proxy, exit IP, which outbound is in use, per-outbound latency). Tiles whose source isn't on the machine (no GPU, no Docker...) simply don't appear. Linux only.
- **Lists scroll** inside their tile (xray outbounds, frp ports, Docker, processes, Ollama models, command output), nothing is cut off.
- **Ollama** shows loaded models (with VRAM) and every available model with its size, in two columns when the tile is wide.
- **Last-hour graphs** on GPU temperature, VRAM, RAM and load show whether a reading is a spike or sustained ("1h · peak 76°"). A small background sampler records one value every 15 s (a few hundred numbers, saved every 5 minutes so a restart keeps them); on short tiles the graph is drawn faintly behind the number. `SYS_SAMPLE_SECONDS=0` turns the sampler off.
- **Folder sizes on drives**: big drive tiles list their largest top-level folders (like `du -sh /mnt/x/*`). The scan runs in the background, one folder at a time, at the lowest CPU/disk priority, local disks only (network shares are skipped; `DU_NETWORK=1` includes them), refreshed every `DU_INTERVAL_HOURS` (default 6, `0` = never) and cached in `data/dirsizes.json`. Untick *Folder sizes* in settings to stop scanning.
- **Hide tiles** you don't want, and individual drives, under *Settings -> Server page*.
- **Command tiles** (like `watch -n 1`): add a title, a command and an interval (1 s and up); its output is shown in a tile and refreshed every N seconds *while someone is looking at the page*. Commands run on the server as the watchcat user, with a time limit and an output cap. Interactive full-screen programs such as `htop` cannot draw there (no terminal) - use the built-in Top CPU / Top memory tiles or e.g. `top -bn1 | head -15`.
- **Privacy:** with a `PASSWORD` set, visitors who are not logged in still see GPU/RAM/VRAM/load/Ollama/drives and the frp/xray *status and ping*, but not process names, Docker details, frp/xray addresses and ports, the xray exit IP, or any command tile (a lock tile tells them to log in). **Command tiles only work when a `PASSWORD` is set** - they execute shell commands, so anyone who can reach an unprotected watchcat must not be able to create them.
- It costs nothing while nobody is on that page: stats are only collected when the page is open (a brief warm-up happens shortly after the dashboard loads), slow sources refresh in the background, and nothing is polled from the dashboard.

Optional settings (`.env`): `OLLAMA_HOST` (default `http://127.0.0.1:11434`), `FRP_CONFIG` / `FRP_SERVICE` (auto-detected from the running `frpc`, service name `frp`), `XRAY_CONFIG` / `XRAY_SERVICE` / `XRAY_PROXY` / `XRAY_TEST_URL` / `XRAY_IP_URL`.

## Install as an app (PWA)

watchcat ships a web-app manifest, icons and a tiny service worker, so it can be added to the home screen and opens full-screen without browser chrome (which also makes the swipe feel natural). The cat icon appears on the home screen; long-press it for a "Server stats" shortcut.

- **Android / Chrome**: menu -> *Install app* / *Add to Home screen*. Chrome only offers a real install (standalone window) on **HTTPS** (or `localhost`). Over plain `http://192.168.x.x:8888` it can only create a shortcut that still opens in a browser tab. If you reach watchcat through an HTTPS reverse proxy / DDNS address, install from that address.
- **iPhone / Safari**: Share -> *Add to Home Screen*. The page declares itself full-screen capable and pads for the notch and home bar (not tested on a real iPhone).
- If the server is unreachable the app shows a small "Can't reach watchcat" page with a Retry button instead of a browser error. Nothing is cached, so you always see the live page.
- Re-create the icons with `python3 tools/make_icons.py` (standard library only).

## Password (optional)

Create `.env` (see `.env.example`):

```
PASSWORD=your-secret
```

- `PASSWORD` set: everyone can **view** the dashboard, but changing services or touching backups requires logging in (the login form appears in the settings panel).
- No `.env` or empty `PASSWORD`: no login anywhere.

Logins survive restarts (stored as hashes in `data/sessions.json`, mode 600) and last `SESSION_DAYS` days (default 14); changing the password logs everyone out. Other options: `PORT` (8888), `HOST` (0.0.0.0), `CHECK_INTERVAL` seconds (30), `DATA_DIR` (`./data`). Real environment variables override `.env`.

## Backups

Settings panel -> Backups. Defaults: folder `/mnt/ssd/backups/watchcat`, every **48 h**, keep **1**. Files are `watchcat-YYYYmmdd-HHMMSS.json`.

- **Backup now**, or **Download** the current list as a file.
- **Restore**: pick one of the backups found in the folder, or upload a file.
- Empty configurations are never backed up, so a wipe can't replace your only good backup.
- If the folder is unavailable (unmounted disk, permissions) the panel shows the error; nothing crashes.

Backups include services, group names/colors and the server-page settings (icons are re-fetched; outage history is not included). Restoring replaces the service list and groups; backup settings are kept.

## Behind a reverse proxy

Works as-is. When serving over HTTPS, forward `X-Forwarded-Proto: https` so the session cookie gets the `Secure` flag.

## Files

`server.py` (HTTP API, checker, backups) - `sysinfo.py` (server-page collectors) - `static/index.html` (whole UI) - `data/` (your services, git-ignored).
