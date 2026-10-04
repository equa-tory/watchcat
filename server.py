#!/usr/bin/env python3
"""watchcat - tiny service status dashboard. Python 3 standard library only."""
import gzip
import hmac
import json
import os
import re
import secrets
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
from urllib.parse import urlparse

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
PASSWORD = cfg("PASSWORD")
HOST = cfg("HOST", "0.0.0.0")
PORT = int(cfg("PORT", "8080"))
CHECK_INTERVAL = max(5, int(cfg("CHECK_INTERVAL", "30")))
MAX_BODY = 2 * 1024 * 1024
MAX_SERVICES = 500
SESSION_TTL = 30 * 24 * 3600

lock = threading.RLock()
services = []  # [{id, name, url, icon}]
status = {}  # id -> {up: bool|None, ms: int|None}
settings = {"path": "/mnt/ssd/backups/watchcat", "interval_hours": 48, "max_backups": 1}
backup_error = ""
sessions = {}  # token -> expiry
wake = threading.Event()


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


# ---------- validation ----------

def clean_service(d, sid=None):
    if not isinstance(d, dict):
        raise Err(400, "Invalid service")
    url = str(d.get("url", "")).strip()[:500]
    if url and "://" not in url:
        url = "http://" + url
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Err(400, "Invalid URL")
    name = str(d.get("name", "")).strip()[:60] or u.hostname
    icon = str(d.get("icon") or "").strip()[:8]
    return {"id": sid or secrets.token_hex(4), "name": name, "url": url, "icon": icon}


def clean_services(items):
    if not isinstance(items, list) or len(items) > MAX_SERVICES:
        raise Err(400, "Invalid backup file")
    out, seen = [], set()
    for it in items:
        sid = it.get("id") if isinstance(it, dict) else None
        if not (isinstance(sid, str) and re.fullmatch(r"[0-9a-f]{8}", sid)) or sid in seen:
            sid = None
        s = clean_service(it, sid)
        seen.add(s["id"])
        out.append(s)
    return out


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


def probe(url):
    """Return (up, ms). Any HTTP answer below 500 counts as up."""
    for method in ("HEAD", "GET"):
        t = time.monotonic()
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": "watchcat"})
            with urllib.request.urlopen(req, timeout=5, context=_ctx):
                pass
            return True, int((time.monotonic() - t) * 1000)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 405:
                return True, int((time.monotonic() - t) * 1000)
            # HEAD refused or broken: retry once with GET
        except Exception:
            return False, None
    return False, None


def checker():
    pool = ThreadPoolExecutor(16)
    while True:
        wake.clear()
        with lock:
            items = [(s["id"], s["url"]) for s in services]
        futs = {pool.submit(probe, url): sid for sid, url in items}
        for f in as_completed(futs):
            up, ms = f.result()
            with lock:
                if futs[f] in {s["id"] for s in services}:
                    status[futs[f]] = {"up": up, "ms": ms}
        wake.wait(CHECK_INTERVAL)


# ---------- backups ----------

BACKUP_RE = re.compile(r"^watchcat-\d{8}-\d{6}\.json$")


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
    with lock:
        snap = list(services)
    if not snap:
        raise Err(400, "Nothing to back up")
    p = settings["path"]
    os.makedirs(p, exist_ok=True)
    name = datetime.now().strftime("watchcat-%Y%m%d-%H%M%S.json")
    save_json(os.path.join(p, name), {"app": "watchcat", "version": 1,
                                      "created": datetime.now().isoformat(timespec="seconds"),
                                      "services": snap})
    for old in list_backups()[max(1, int(settings["max_backups"])):]:
        os.remove(os.path.join(p, old["name"]))
    return name


def restore(data):
    new = clean_services(data.get("services") if isinstance(data, dict) else None)
    with lock:
        services[:] = new
        for k in [k for k in status if k not in {s["id"] for s in new}]:
            del status[k]
        persist()
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
        svc = [{**s, **status.get(s["id"], {"up": None, "ms": None})} for s in services]
    return {"services": svc, "auth_required": bool(PASSWORD), "authed": authed}


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
        if p[:1] == ["backup"]:
            return self.backup(m, p[1:], raw)
        raise Err(404, "Not found")

    def services(self, m, p, raw):
        with lock:
            if m == "POST" and not p:
                if len(services) >= MAX_SERVICES:
                    raise Err(400, "Too many services")
                s = clean_service(self.body(raw))
                services.append(s)
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
                services[i] = clean_service(self.body(raw), p[0])
                status.pop(p[0], None)
            elif m == "DELETE" and len(p) == 1:
                services[:] = [s for s in services if s["id"] != p[0]]
                status.pop(p[0], None)
            else:
                raise Err(404, "Not found")
            persist()
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
                save_json(SETTINGS_FILE, settings)
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
            with lock:
                data = json.dumps({"app": "watchcat", "version": 1, "services": services}, indent=2).encode()
            name = datetime.now().strftime("watchcat-%Y%m%d-%H%M%S.json")
            return self.reply(200, data, headers=[("Content-Disposition", f'attachment; filename="{name}"')])
        raise Err(404, "Not found")


def main():
    os.makedirs(DATA, exist_ok=True)
    services[:] = clean_services(load_json(SERVICES_FILE, []))
    try:
        settings.update(clean_settings({**settings, **load_json(SETTINGS_FILE, {})}))
    except Err:
        pass
    threading.Thread(target=checker, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"watchcat on http://{HOST}:{PORT}  (auth: {'on' if PASSWORD else 'off'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
