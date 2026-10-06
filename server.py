#!/usr/bin/env python3
"""watchcat - tiny service status dashboard. Python 3 standard library only."""
import getpass
import gzip
import hmac
import json
import os
import queue
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse

import sysinfo

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env[k.strip()] = v
    except OSError:
        pass
    return env


_ENV = load_env(os.path.join(ROOT, ".env"))


def cfg(key, default=""):
    return os.environ.get(key, _ENV.get(key, default))


DATA = os.path.abspath(cfg("DATA_DIR", os.path.join(ROOT, "data")))
SERVICES_FILE = os.path.join(DATA, "services.json")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
ICONS = os.path.join(DATA, "icons")
HIST_FILE = os.path.join(DATA, "history.json")
SERVER_FILE = os.path.join(DATA, "servertiles.json")
PASSWORD = cfg("PASSWORD")
HOST = cfg("HOST", "0.0.0.0")
PORT = int(cfg("PORT", "8888"))
CHECK_INTERVAL = max(5, int(cfg("CHECK_INTERVAL", "30")))
MAX_BODY = 2 * 1024 * 1024
MAX_SERVICES = 500
MAX_ADDRS = 16
SESSION_TTL = 30 * 24 * 3600

lock = threading.RLock()
services = []  # [{id, name, icon, group, addrs:[{url,label,tcp}], fav?, fav_try?}]
status = {}  # id -> [{up: bool|None, ms: int|None}] aligned with addrs
groups = {}  # "3" -> {name, color}
settings = {"path": "/mnt/ssd/backups/watchcat", "interval_hours": 48, "max_backups": 1}
backup_error = ""
sessions = {}  # token -> expiry
server_tiles = {"hidden": [], "hide_mounts": [], "cmds": []}  # Server page: hidden tiles, hidden mounts, command tiles
history = {}  # id -> [[ts, state], ...]  state changes only: 0 down, 1 up, 2 partial, 3 unknown (not monitored)
hist_v = 0  # bumped on every recorded change so clients know when to refetch
_cand = {}  # id -> (state, first_seen, count): a change must be seen twice in a row before it is recorded
_hist_saved = 0.0
HIST_DAYS = 31
MAX_EVENTS = 400
wake = threading.Event()
icon_q = queue.Queue()


class Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ---------- persistence ----------

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        os.replace(path, path + ".corrupt")  # keep it, start fresh
        return default


def persist():
    save_json(SERVICES_FILE, services)


def save_settings():
    save_json(SETTINGS_FILE, {**settings, "groups": groups})


# ---------- validation ----------

ICON_TYPES = {"image/png", "image/x-icon", "image/gif", "image/jpeg", "image/webp", "image/svg+xml"}
_TCP_RE = re.compile(r"(?:[a-z][a-z0-9+.-]*://)?(\[[0-9a-fA-F:.]+\]|[^\s/:\[\]]+):(\d{1,5})/?", re.I)


def clean_addr(a):
    if isinstance(a, str):
        a = {"url": a}
    if not isinstance(a, dict):
        raise Err(400, "Invalid address")
    raw = str(a.get("url", "")).strip()[:500]
    label = str(a.get("label") or "").strip()[:80]
    if a.get("tcp"):
        m = _TCP_RE.fullmatch(raw)
        if not m or not 1 <= int(m.group(2)) <= 65535:
            raise Err(400, "Port address must look like host:port")
        return {"url": f"{m.group(1)}:{int(m.group(2))}", "label": label, "tcp": True}
    if raw and "://" not in raw:
        raw = "http://" + raw
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Err(400, "Invalid URL")
    return {"url": raw, "label": label, "tcp": False}


def clean_service(d, sid=None):
    if not isinstance(d, dict):
        raise Err(400, "Invalid service")
    addrs = d.get("addrs")
    if addrs is None and d.get("url"):  # pre-multi-address data
        addrs = [{"url": d["url"]}]
    if not isinstance(addrs, list) or not 1 <= len(addrs) <= MAX_ADDRS:
        raise Err(400, "Add 1-%d addresses" % MAX_ADDRS)
    addrs = [clean_addr(a) for a in addrs]
    first = addrs[0]["url"]
    name = str(d.get("name", "")).strip()[:60] or (first.rsplit(":", 1)[0] if addrs[0]["tcp"] else urlparse(first).hostname)
    icon = str(d.get("icon") or "").strip()[:8]
    try:
        group = int(d.get("group") or 0)
    except (TypeError, ValueError):
        raise Err(400, "Group must be a number")
    if not 0 <= group <= 999:
        raise Err(400, "Group 0-999")
    return {"id": sid or secrets.token_hex(4), "name": name, "icon": icon, "group": group, "addrs": addrs}


def keep_meta(src, dst):
    """Copy favicon bookkeeping (not user input) from src to dst."""
    fav = src.get("fav")
    if isinstance(fav, dict) and fav.get("type") in ICON_TYPES and isinstance(fav.get("v"), int):
        dst["fav"] = {"type": fav["type"], "v": fav["v"]}
    if isinstance(src.get("fav_try"), (int, float)):
        dst["fav_try"] = src["fav_try"]


def web_urls(s):
    return [a["url"] for a in s["addrs"] if not a["tcp"]]


def clean_services(items, meta=False):
    if not isinstance(items, list) or len(items) > MAX_SERVICES:
        raise Err(400, "Invalid backup file")
    out, seen = [], set()
    for it in items:
        sid = it.get("id") if isinstance(it, dict) else None
        if not (isinstance(sid, str) and re.fullmatch(r"[0-9a-f]{8}", sid)) or sid in seen:
            sid = None
        s = clean_service(it, sid)
        if meta:
            keep_meta(it, s)
        seen.add(s["id"])
        out.append(s)
    return out


def clean_groups(d):
    if not isinstance(d, dict) or len(d) > 1000:
        raise Err(400, "Invalid groups")
    out = {}
    for k, v in d.items():
        if not (isinstance(k, str) and k.isdigit() and 1 <= int(k) <= 999) or not isinstance(v, dict):
            continue
        name = str(v.get("name") or "").strip()[:30]
        color = str(v.get("color") or "").strip()
        if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise Err(400, "Invalid color")
        if name or color:
            out[str(int(k))] = {"name": name, "color": color.lower()}
    return out


def clean_server_tiles(d, strict=True):
    """Validate Server-page config. Command tiles run shell commands, so they need a PASSWORD (strict: error, else dropped)."""
    if not isinstance(d, dict):
        raise Err(400, "Invalid server tiles")
    hidden = [h for h in d.get("hidden") or [] if h in sysinfo.BUILTIN]
    mounts = [str(m)[:200] for m in (d.get("hide_mounts") or [])[:50] if str(m).startswith("/")]
    cmds, seen = [], set()
    for c in (d.get("cmds") or []):
        if not isinstance(c, dict):
            raise Err(400, "Invalid command tile")
        cmd = str(c.get("cmd") or "").strip()[:500]
        if not cmd:
            continue
        try:
            interval = 5.0 if c.get("interval") in (None, "") else float(c["interval"])
        except (TypeError, ValueError):
            raise Err(400, "Interval must be a number of seconds")
        if not 1 <= interval <= 3600:
            raise Err(400, "Interval 1-3600 seconds")
        cid = c.get("id") if isinstance(c.get("id"), str) and re.fullmatch(r"[0-9a-f]{8}", c["id"]) and c["id"] not in seen else secrets.token_hex(4)
        seen.add(cid)
        cmds.append({"id": cid, "name": str(c.get("name") or "").strip()[:40] or "Command", "cmd": cmd,
                     "interval": int(interval) if interval == int(interval) else round(interval, 1), "wide": bool(c.get("wide", True))})
    if len(cmds) > 12:
        raise Err(400, "At most 12 command tiles")
    if cmds and not PASSWORD:
        if strict:
            raise Err(403, "Set PASSWORD in .env first: command tiles run shell commands on this server")
        cmds = []
    return {"hidden": hidden, "hide_mounts": mounts, "cmds": cmds}


def apply_server_tiles():
    sysinfo.configure({**server_tiles, "cmds_enabled": bool(PASSWORD)})


def clean_settings(d):
    path = str(d.get("path", "")).strip()
    if not path or len(path) > 300:
        raise Err(400, "Invalid path")
    try:
        interval = float(d.get("interval_hours"))
        keep = int(d.get("max_backups"))
    except (TypeError, ValueError):
        raise Err(400, "Invalid number")
    if not 1 <= interval <= 8760 or not 1 <= keep <= 100:
        raise Err(400, "Interval 1-8760 h, backups 1-100")
    return {"path": path, "interval_hours": int(interval) if interval == int(interval) else interval, "max_backups": keep}


# ---------- status checker ----------

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE  # self-signed homelab certs are common
# Always connect directly: a monitor must not route LAN checks through an http_proxy from the environment.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_ctx))


def probe(url):
    """Return (up, ms). Any HTTP answer below 500 counts as up."""
    for method in ("HEAD", "GET"):
        t = time.monotonic()
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": "watchcat"})
            with _opener.open(req, timeout=5):
                pass
            return True, int((time.monotonic() - t) * 1000)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 405:
                return True, int((time.monotonic() - t) * 1000)
            # HEAD refused or broken: retry once with GET
        except Exception:
            return False, None
    return False, None


def probe_tcp(hostport):
    host, port = hostport.rsplit(":", 1)
    t = time.monotonic()
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=5):
            pass
        return True, int((time.monotonic() - t) * 1000)
    except OSError:
        return False, None


def checker():
    pool = ThreadPoolExecutor(16)
    while True:
        wake.clear()
        futs = {}
        with lock:
            for s in services:
                for i, a in enumerate(s["addrs"]):
                    futs[pool.submit(probe_tcp if a["tcp"] else probe, a["url"])] = (s["id"], i, a["url"])
        for f in as_completed(futs):
            up, ms = f.result()
            sid, i, url = futs[f]
            with lock:
                s = next((s for s in services if s["id"] == sid), None)
                if s and i < len(s["addrs"]) and s["addrs"][i]["url"] == url:  # not edited meanwhile
                    st = status.setdefault(sid, [])
                    del st[len(s["addrs"]):]
                    while len(st) < len(s["addrs"]):
                        st.append({"up": None, "ms": None})
                    st[i] = {"up": up, "ms": ms}
        try:
            record_history(time.time())
        except Exception as e:
            sys.stderr.write("history error: %r\n" % (e,))
        wake.wait(CHECK_INTERVAL)


# ---------- outage history ----------
# Only state *changes* are stored (a few entries per day), never one sample per check, so the file stays
# tiny, the API payload is small and drawing a tile's timeline costs a handful of SVG rects.

def overall_state(s):
    st = status.get(s["id"])
    if not st or len(st) != len(s["addrs"]) or any(x["up"] is None for x in st):
        return None  # not fully checked yet
    ups = sum(1 for x in st if x["up"])
    return 1 if ups == len(st) else 0 if ups == 0 else 2


def save_history(now):
    global _hist_saved
    save_json(HIST_FILE, {"alive": now, "ev": history})
    _hist_saved = now


def record_history(now):
    global hist_v
    changed = False
    with lock:
        ids = {s["id"] for s in services}
        for sid in [k for k in history if k not in ids]:
            del history[sid]
            changed = True
        for sid in [k for k in _cand if k not in ids]:
            del _cand[sid]
        for s in services:
            sid, cur = s["id"], overall_state(s)
            if cur is None:
                continue
            ev = history.setdefault(sid, [])
            last = ev[-1][1] if ev else None
            if cur == last:
                _cand.pop(sid, None)
                continue
            c = _cand.get(sid)
            c = (cur, c[1], c[2] + 1) if c and c[0] == cur else (cur, now, 1)
            _cand[sid] = c
            if last is None or last == 3 or c[2] >= 2:  # first sight / back from not-monitored: no debounce
                ev.append([int(max(c[1], ev[-1][0] if ev else 0)), cur])
                del _cand[sid]
                changed = True
        cutoff = now - HIST_DAYS * 86400
        for ev in history.values():
            i = next((i for i, e in enumerate(ev) if e[0] >= cutoff), len(ev))
            if i > 1:  # keep one older event as the anchor for the window start
                del ev[:i - 1]
            del ev[:-MAX_EVENTS]
        if changed:
            hist_v += 1
        if changed or now - _hist_saved > 300:  # periodic save also refreshes "alive"
            save_history(now)


def load_history():
    raw = load_json(HIST_FILE, {})
    now = time.time()
    ev = raw.get("ev") if isinstance(raw, dict) else None
    alive = raw.get("alive") if isinstance(raw, dict) else None
    alive = min(alive, now) if isinstance(alive, (int, float)) else now
    with lock:
        ids = {s["id"] for s in services}
        for sid, e in (ev.items() if isinstance(ev, dict) else []):
            if sid not in ids or not isinstance(e, list):
                continue
            ok = [[int(t), st] for t, st in (x for x in e if isinstance(x, list) and len(x) == 2)
                  if isinstance(t, (int, float)) and st in (0, 1, 2, 3)]
            if ok:
                if ok[-1][1] != 3:  # watchcat was not running since `alive`: mark that gap as unknown
                    ok.append([int(max(alive, ok[-1][0])), 3])
                history[sid] = ok[-MAX_EVENTS:]


# ---------- favicons ----------

_LINK = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r"([a-zA-Z_:][-\w:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
ICON_MAX = 256 * 1024


def sniff(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if b[:3] == b"GIF":
        return "image/gif"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b"<svg" in b[:2048].lower():
        return "image/svg+xml"
    return None


def http_get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 watchcat"})
    with _opener.open(req, timeout=5) as r:
        return r.read(limit + 1), r.geturl()


def icon_candidates(html, base):
    out = []
    for tag in _LINK.findall(html):
        at = {k.lower(): v.strip("\"'") for k, v in _ATTR.findall(tag)}
        rel = at.get("rel", "").lower().split()
        href = at.get("href")
        if not href or href.startswith("data:") or not ("icon" in rel or "apple-touch-icon" in rel):
            continue
        url = urljoin(base, href)
        if urlparse(url).scheme not in ("http", "https"):
            continue
        score = 2.0 if "apple-touch-icon" in rel else 0.0
        if url.split("?")[0].lower().endswith(".svg") or "svg" in at.get("type", ""):
            score += 1
        m = re.match(r"(\d+)x(\d+)", at.get("sizes", ""))
        if m:
            score += min(int(m.group(1)), 256) / 256
        out.append((score, url))
    out.sort(key=lambda x: -x[0])
    return [u for _, u in out]


def fetch_icon(urls):
    """Find a favicon for the first web address that has one -> (bytes, content-type) | None."""
    for base in urls[:3]:
        final, cands = base, []
        try:
            page, final = http_get(base, 262144)
            cands = icon_candidates(page.decode("utf-8", "ignore"), final)[:3]
        except Exception:
            pass
        for url in cands + [urljoin(final, "/favicon.ico")]:
            try:
                data, _ = http_get(url, ICON_MAX)
            except Exception:
                continue
            ctype = sniff(data) if len(data) <= ICON_MAX else None
            if ctype:
                return data, ctype
    return None


def fetch_for(sid):
    with lock:
        s = next((s for s in services if s["id"] == sid), None)
        urls = web_urls(s) if s else []
    res = fetch_icon(urls) if urls else None
    with lock:
        s = next((s for s in services if s["id"] == sid), None)
        if not s or web_urls(s) != urls:  # deleted or re-addressed meanwhile (a new fetch is queued)
            return
        if res:
            os.makedirs(ICONS, exist_ok=True)
            tmp = os.path.join(ICONS, sid + ".tmp")
            with open(tmp, "wb") as f:
                f.write(res[0])
            os.replace(tmp, os.path.join(ICONS, sid))
            s["fav"] = {"type": res[1], "v": int(time.time())}
        else:
            s["fav_try"] = time.time()
        persist()


def icon_scan():
    now = time.time()
    with lock:
        ids = [s["id"] for s in services if "fav" not in s and now - s.get("fav_try", 0) > 86400]
    for sid in ids:
        icon_q.put(sid)


def icon_worker():
    try:
        with lock:
            keep = {s["id"] for s in services}
        for n in os.listdir(ICONS):
            if n not in keep:
                os.remove(os.path.join(ICONS, n))
    except OSError:
        pass
    icon_scan()
    while True:
        try:
            sid = icon_q.get(timeout=3600)
        except queue.Empty:
            icon_scan()
            continue
        try:
            fetch_for(sid)
        except Exception as e:
            sys.stderr.write("icon error: %r\n" % (e,))


# ---------- backups ----------

BACKUP_RE = re.compile(r"^watchcat-\d{8}-\d{6}\.json$")


def snapshot():
    with lock:
        svc = [{k: v for k, v in s.items() if k not in ("fav", "fav_try")} for s in services]
        return {"app": "watchcat", "version": 2, "services": svc, "groups": dict(groups), "server": dict(server_tiles)}


def list_backups():
    p = settings["path"]
    try:
        names = [n for n in os.listdir(p) if BACKUP_RE.match(n)]
    except FileNotFoundError:
        return []
    out = []
    for n in sorted(names, reverse=True):
        st = os.stat(os.path.join(p, n))
        out.append({"name": n, "size": st.st_size, "time": int(st.st_mtime)})
    return out


def make_backup():
    snap = snapshot()
    if not snap["services"]:
        raise Err(400, "Nothing to back up")
    snap["created"] = datetime.now().isoformat(timespec="seconds")
    p = settings["path"]
    os.makedirs(p, exist_ok=True)
    name = datetime.now().strftime("watchcat-%Y%m%d-%H%M%S.json")
    save_json(os.path.join(p, name), snap)
    for old in list_backups()[max(1, int(settings["max_backups"])):]:
        os.remove(os.path.join(p, old["name"]))
    return name


def restore(data):
    if not isinstance(data, dict):
        raise Err(400, "Invalid backup file")
    new = clean_services(data.get("services"))
    new_groups = clean_groups(data["groups"]) if "groups" in data else None
    new_server = clean_server_tiles(data["server"], strict=False) if isinstance(data.get("server"), dict) else None
    with lock:
        old = {s["id"]: s for s in services}
        for s in new:
            o = old.get(s["id"])
            if o and web_urls(o) == web_urls(s):
                keep_meta(o, s)  # same service, icon already fetched
        services[:] = new
        for k in [k for k in status if k not in {s["id"] for s in new}]:
            del status[k]
        persist()
        if new_groups is not None:
            groups.clear()
            groups.update(new_groups)
            save_settings()
        if new_server is not None:
            server_tiles.update(new_server)
            save_json(SERVER_FILE, server_tiles)
            apply_server_tiles()
    icon_scan()
    wake.set()


def backup_loop():
    global backup_error
    time.sleep(10)
    while True:
        try:
            b = list_backups()
            newest = b[0]["time"] if b else 0
            if services and time.time() - newest >= settings["interval_hours"] * 3600:
                make_backup()
            backup_error = ""
        except Exception as e:  # unmounted disk, permissions, ...
            backup_error = str(e)
        time.sleep(60)


# ---------- http ----------

with open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8") as _f:
    PAGE = _f.read()


def state(authed):
    with lock:
        svc = []
        for s in services:
            st = status.get(s["id"])
            if not st or len(st) != len(s["addrs"]):
                st = [{"up": None, "ms": None}] * len(s["addrs"])
            o = {k: s[k] for k in ("id", "name", "icon", "group", "addrs")}
            o["st"] = st
            o["fav"] = s["fav"]["v"] if "fav" in s else None
            svc.append(o)
        return {"services": svc, "groups": dict(groups), "hv": hist_v, "sys": sysinfo.SUPPORTED, "auth_required": bool(PASSWORD), "authed": authed}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "watchcat"
    timeout = 30

    def log_message(self, *a):
        pass

    def reply(self, code, body=b"", ctype="application/json", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not any(k.lower() == "cache-control" for k, _ in headers):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code=200, headers=()):
        self.reply(code, json.dumps(obj).encode(), headers=headers)

    def authed(self):
        if not PASSWORD:
            return True
        c = SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["wc_session"].value if "wc_session" in c else ""
        return sessions.get(tok, 0) > time.time()

    def do_GET(self):
        self.route()

    do_POST = do_PUT = do_DELETE = do_GET

    def route(self):
        try:
            m = self.command
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.close_connection = True
                raise Err(413, "Too large")
            raw = self.rfile.read(n) if n else b""
            if m != "GET" and self.headers.get("X-WC") != "1":
                raise Err(403, "Forbidden")
            self.dispatch(m, [p for p in path.split("/") if p], raw)
        except Err as e:
            self.json({"error": e.msg}, e.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            sys.stderr.write("error: %r\n" % (e,))
            self.json({"error": "Server error"}, 500)

    def body(self, raw):
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            raise Err(400, "Bad JSON")
        if not isinstance(d, dict):
            raise Err(400, "Bad JSON")
        return d

    def icon(self, sid):
        with lock:
            s = next((s for s in services if s["id"] == sid), None)
            fav = s.get("fav") if s else None
        if not fav:
            raise Err(404, "No icon")
        try:
            with open(os.path.join(ICONS, sid), "rb") as f:  # sid is an existing service id, never user path
                data = f.read()
        except OSError:
            raise Err(404, "No icon")
        self.reply(200, data, fav["type"], [("Cache-Control", "public, max-age=604800"),
                                            ("Content-Security-Policy", "sandbox")])

    def dispatch(self, m, p, raw):
        if m == "GET" and not p:
            html = PAGE.replace("__STATE__", json.dumps(state(self.authed())).replace("</", "<\\/"))
            data, hdr = html.encode(), []
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                data, hdr = gzip.compress(data, 6), [("Content-Encoding", "gzip")]
            return self.reply(200, data, "text/html; charset=utf-8", hdr)
        if p[:1] != ["api"]:
            raise Err(404, "Not found")
        p = p[1:]

        if m == "GET" and p == ["state"]:
            return self.json(state(self.authed()))
        if m == "GET" and len(p) == 2 and p[0] == "icon":
            return self.icon(p[1])
        if m == "GET" and p == ["sys"]:
            if not sysinfo.SUPPORTED:
                return self.json({"supported": False})
            full = self.authed()  # no PASSWORD set -> everyone counts as logged in
            peek = "peek=1" in (urlparse(self.path).query or "")
            return self.json({**sysinfo.collect(full, peek), "redacted": not full, "auth_required": bool(PASSWORD)})
        if m == "GET" and p == ["sys-hist"]:
            return self.json(sysinfo.history() if sysinfo.SUPPORTED else {"t": [], "s": {}, "now": time.time(), "step": 15})
        if m == "GET" and p == ["history"]:
            with lock:
                return self.json({"now": time.time(), "ev": history})
        if m == "POST" and p == ["login"]:
            ok = hmac.compare_digest(str(self.body(raw).get("password", "")).encode(), PASSWORD.encode())
            if not PASSWORD or not ok:
                time.sleep(1)
                raise Err(401, "Wrong password")
            now = time.time()
            for k in [k for k, v in sessions.items() if v < now]:
                del sessions[k]
            tok = secrets.token_urlsafe(32)
            sessions[tok] = now + SESSION_TTL
            sec = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return self.json({"ok": True}, headers=[(
                "Set-Cookie", f"wc_session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}{sec}")])
        if m == "POST" and p == ["logout"]:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            if "wc_session" in c:
                sessions.pop(c["wc_session"].value, None)
            return self.json({"ok": True}, headers=[("Set-Cookie", "wc_session=; Max-Age=0; Path=/")])

        if not self.authed():
            raise Err(401, "Login required")

        if p[:1] == ["services"]:
            return self.services(m, p[1:], raw)
        if m == "PUT" and p == ["groups"]:
            new = clean_groups(self.body(raw).get("groups"))
            with lock:
                groups.clear()
                groups.update(new)
                save_settings()
            return self.json(state(True))
        if p == ["server-tiles"]:
            if m == "GET":
                return self.json({**server_tiles, "builtin": sysinfo.BUILTIN, "cmds_allowed": bool(PASSWORD),
                                  "user": getpass.getuser()})
            if m == "PUT":
                new = clean_server_tiles(self.body(raw))
                with lock:
                    server_tiles.update(new)
                    save_json(SERVER_FILE, server_tiles)
                    apply_server_tiles()
                return self.json({**server_tiles, "builtin": sysinfo.BUILTIN, "cmds_allowed": bool(PASSWORD),
                                  "user": getpass.getuser()})
        if p[:1] == ["backup"]:
            return self.backup(m, p[1:], raw)
        raise Err(404, "Not found")

    def services(self, m, p, raw):
        removed = None
        with lock:
            if m == "POST" and not p:
                if len(services) >= MAX_SERVICES:
                    raise Err(400, "Too many services")
                s = clean_service(self.body(raw))
                services.append(s)
                icon_q.put(s["id"])
            elif m == "POST" and p == ["reorder"]:
                ids = self.body(raw).get("ids")
                by = {s["id"]: s for s in services}
                if not isinstance(ids, list) or set(ids) != set(by) or len(ids) != len(by):
                    raise Err(400, "Bad order")
                services[:] = [by[i] for i in ids]
            elif m == "PUT" and len(p) == 1:
                i = next((i for i, s in enumerate(services) if s["id"] == p[0]), None)
                if i is None:
                    raise Err(404, "Not found")
                s = clean_service(self.body(raw), p[0])
                if web_urls(s) == web_urls(services[i]):
                    keep_meta(services[i], s)
                else:
                    icon_q.put(p[0])
                services[i] = s
                status.pop(p[0], None)
            elif m == "DELETE" and len(p) == 1:
                services[:] = [s for s in services if s["id"] != p[0]]
                status.pop(p[0], None)
                removed = p[0]
            else:
                raise Err(404, "Not found")
            persist()
        if removed and re.fullmatch(r"[0-9a-f]{8}", removed):
            try:
                os.remove(os.path.join(ICONS, removed))
            except OSError:
                pass
        wake.set()
        self.json(state(True))

    def backup(self, m, p, raw):
        if m == "GET" and not p:
            try:
                backups, err = list_backups(), backup_error
            except OSError as e:
                backups, err = [], str(e)
            return self.json({"settings": settings, "backups": backups, "error": err})
        if m == "PUT" and p == ["settings"]:
            new = clean_settings(self.body(raw))
            with lock:
                settings.update(new)
                save_settings()
            return self.json({"ok": True})
        if m == "POST" and p == ["now"]:
            try:
                return self.json({"name": make_backup()})
            except OSError as e:
                raise Err(500, str(e))
        if m == "POST" and len(p) == 2 and p[0] == "restore":
            if not BACKUP_RE.match(p[1]):
                raise Err(400, "Bad name")
            try:
                with open(os.path.join(settings["path"], p[1]), encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                raise Err(404, "Cannot read backup: %s" % e)
            restore(data)
            return self.json({"ok": True})
        if m == "POST" and p == ["upload"]:
            restore(self.body(raw))
            return self.json({"ok": True})
        if m == "GET" and p == ["download"]:
            data = json.dumps(snapshot(), indent=2, ensure_ascii=False).encode()
            name = datetime.now().strftime("watchcat-%Y%m%d-%H%M%S.json")
            return self.reply(200, data, headers=[("Content-Disposition", f'attachment; filename="{name}"')])
        raise Err(404, "Not found")


def main():
    os.makedirs(DATA, exist_ok=True)
    services[:] = clean_services(load_json(SERVICES_FILE, []), meta=True)
    raw = load_json(SETTINGS_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    try:
        settings.update(clean_settings({**settings, **raw}))
        groups.update(clean_groups(raw.get("groups", {})))
    except Err:
        pass
    persist()  # writes back migrated (multi-address) format
    load_history()
    raw = load_json(SERVER_FILE, {})
    try:
        server_tiles.update(clean_server_tiles(raw, strict=False))
    except Err:
        pass
    sysinfo.init(cfg, DATA)
    apply_server_tiles()
    threading.Thread(target=checker, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()
    threading.Thread(target=icon_worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"watchcat on http://{HOST}:{PORT}  (auth: {'on' if PASSWORD else 'off'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
