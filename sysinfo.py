"""Host statistics for watchcat's "Server" page. Linux only, standard library only.

Nothing runs unless somebody asks (/api/sys). Cheap sources (/proc) are read inline; anything slow or
fragile (nvidia-smi, docker, hung network mounts, frp/xray probes) is refreshed in a background thread and
the last good value is served meanwhile, so a request never waits on them. A missing source (no GPU, no
docker...) simply yields None and its tile is not shown.
"""
import ipaddress
import json
import os
import re
import select
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

SUPPORTED = sys.platform.startswith("linux")
_get = lambda key, default="": os.environ.get(key, default)  # replaced by init() with server.cfg
_lock = threading.Lock()

# tile ids the user can hide
BUILTIN = ["cpu", "ram", "gpu", "vram", "ollama", "drives", "docker", "procs_cpu", "procs_mem", "frp", "xray"]
conf = {"hidden": [], "hide_mounts": [], "cmds": []}  # set by server via configure()
DEFAULT_HIDE_MOUNTS = ["/boot", "/boot/efi"]


def init(getter):
    global _get
    _get = getter
    if SUPPORTED:
        threading.Thread(target=_cmd_loop, daemon=True).start()


def configure(c):
    conf.update(c)


# ---------- caching helpers ----------

class _Slot:
    __slots__ = ("val", "ts", "busy", "ev")

    def __init__(self):
        self.val, self.ts, self.busy, self.ev = None, -1e9, False, threading.Event()


_slots = {}


_budget_end = 0.0  # collect() gives all first-time collectors one shared wait budget


def bg(key, ttl, fn, wait=True):
    """Last known value of fn(); refreshed in the background when older than ttl seconds."""
    with _lock:
        s = _slots.setdefault(key, _Slot())
        start = (time.monotonic() - s.ts >= ttl) and not s.busy
        if start:
            s.busy = True

    if start:
        def run():
            try:
                v = fn()
            except Exception:
                v = None
            with _lock:
                s.val, s.ts, s.busy = v, time.monotonic(), False
            s.ev.set()
        threading.Thread(target=run, daemon=True).start()
    if wait and not s.ev.is_set():  # very first request: give the collector a moment (shared budget)
        s.ev.wait(max(0.0, _budget_end - time.monotonic()))
    return s.val


_memo = {}


def memo(key, ttl, fn):
    """Synchronous cache for use *inside* background collectors."""
    now = time.monotonic()
    hit = _memo.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        v = fn()
    except Exception:
        v = None
    _memo[key] = (now, v)
    return v


# ---------- cpu / ram ----------

_cpu_prev = None


def _cpu_sample():
    with open("/proc/stat") as f:
        v = list(map(int, f.readline().split()[1:]))
    return sum(v), v[3] + v[4], time.monotonic()  # total, idle+iowait


def cpu():
    global _cpu_prev
    cur = _cpu_sample()
    prev = _cpu_prev
    if prev is None or cur[2] - prev[2] > 10:  # no recent baseline: take a short one
        time.sleep(0.25)
        prev, cur = cur, _cpu_sample()
    _cpu_prev = cur
    pct = None
    if cur[0] > prev[0]:
        pct = round(100 * (1 - (cur[1] - prev[1]) / (cur[0] - prev[0])), 1)
    return {"pct": pct, "load": [round(x, 2) for x in os.getloadavg()], "cores": os.cpu_count()}


def ram():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0]) * 1024
    total, avail = m["MemTotal"], m.get("MemAvailable", m["MemFree"])
    return {"total": total, "used": total - avail, "swap_total": m.get("SwapTotal", 0),
            "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0)}


# ---------- gpu ----------

def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None  # "[N/A]"


def gpu():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    r = subprocess.run([exe, "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,power.limit",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
    if r.returncode:
        return None
    out = []
    for line in r.stdout.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) >= 7:
            out.append({"name": f[0], "temp": _num(f[1]), "util": _num(f[2]), "mem_used": _num(f[3]),
                        "mem_total": _num(f[4]), "power": _num(f[5]), "power_limit": _num(f[6])})
    return out or None


# ---------- ollama ----------

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never use an environment proxy for local services


def _json_get(url, timeout=2):
    with _direct.open(url, timeout=timeout) as r:
        return json.loads(r.read(2_000_000))


def ollama():
    base = _get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    if "://" not in base:
        base = "http://" + base
    try:
        ps = _json_get(base + "/api/ps")
    except Exception:
        return None  # not running / not reachable
    tags = memo("ollama_tags", 30, lambda: _json_get(base + "/api/tags", 3)) or {}
    loaded = [{"name": m.get("name"), "size": m.get("size"), "vram": m.get("size_vram"), "expires": m.get("expires_at")}
              for m in ps.get("models", [])]
    avail = sorted(({"name": m.get("name"), "size": m.get("size")} for m in tags.get("models", [])), key=lambda m: m["name"] or "")
    return {"loaded": loaded, "available": avail}


# ---------- drives ----------

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "cifs", "smb3", "nfs", "nfs4", "ntfs", "ntfs3", "fuseblk",
           "vfat", "exfat", "f2fs", "jfs", "reiserfs"}
SKIP_PREFIX = ("/var/lib/docker", "/var/lib/containers", "/var/lib/kubelet", "/snap", "/run", "/sys", "/proc", "/dev")
_fs_pool = ThreadPoolExecutor(8, thread_name_prefix="statvfs")
_fs_pending = {}


def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _statvfs(mp):
    """statvfs with a timeout: a dead network mount must not freeze the page."""
    f = _fs_pending.get(mp)
    if f is None or f.done():
        f = _fs_pending[mp] = _fs_pool.submit(os.statvfs, mp)
    try:
        return f.result(timeout=2)
    except FutTimeout:
        return None  # still hung from an earlier attempt
    except OSError:
        raise


def drives():
    seen, out = set(), []
    with open("/proc/self/mounts") as f:
        rows = [l.split()[:3] for l in f if l.strip()]
    for dev, mp, fs in rows:
        mp = _unescape(mp)
        if fs not in REAL_FS or dev in seen or mp.startswith(SKIP_PREFIX):
            continue
        seen.add(dev)
        try:
            st = _statvfs(mp)
        except OSError:
            continue
        if st is None:
            out.append({"mount": mp, "fstype": fs, "stale": True})
            continue
        size = st.f_blocks * st.f_frsize
        if not size:
            continue
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        out.append({"mount": mp, "fstype": fs, "size": size, "used": used, "avail": free,
                    "pct": round(100 * used / max(1, used + free), 1)})
    return sorted(out, key=lambda d: d["mount"])


# ---------- docker ----------

def docker():
    exe = shutil.which("docker")
    if not exe:
        return None
    r = subprocess.run([exe, "ps", "--format", "{{json .}}"], capture_output=True, text=True, timeout=4)
    if r.returncode:
        return None
    out = []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        st = d.get("Status", "")
        out.append({"name": d.get("Names"), "image": d.get("Image"), "status": st, "ports": d.get("Ports", ""),
                    "bad": "unhealthy" in st or "restarting" in st.lower()})
    return sorted(out, key=lambda c: c["name"] or "")


# ---------- processes ----------

_pp = {}   # pid -> cpu ticks at last sample
_pp_t = None
_users = {}
_INTERP = {"python", "python3", "node", "java", "ruby", "perl", "php", "bash", "sh"}


def _user(uid):
    if uid not in _users:
        try:
            import pwd
            _users[uid] = pwd.getpwuid(uid).pw_name
        except Exception:
            _users[uid] = str(uid)
    return _users[uid]


def _pname(pid, comm):
    if comm not in _INTERP and not comm.startswith("python"):
        return comm
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().split(b"\0")
    except OSError:
        return comm
    args = [a.decode("utf-8", "replace") for a in args[1:] if a]
    for i, a in enumerate(args):
        if a == "-m" and i + 1 < len(args):
            return args[i + 1]
        if not a.startswith("-"):
            return os.path.basename(a) or comm
    return comm


def _scan():
    page, out = os.sysconf("SC_PAGE_SIZE"), {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                data = f.read()
            lp, rp = data.index("("), data.rindex(")")
            fl = data[rp + 2:].split()
            out[int(pid)] = (data[lp + 1:rp], int(fl[11]) + int(fl[12]), int(fl[21]) * page)
        except (OSError, ValueError, IndexError):
            continue
    return out


def procs():
    global _pp, _pp_t
    clk = os.sysconf("SC_CLK_TCK")
    now = time.monotonic()
    if _pp_t is None or now - _pp_t > 20:  # no recent baseline
        _pp = {p: v[1] for p, v in _scan().items()}
        _pp_t = now
        time.sleep(0.3)
        now = time.monotonic()
    cur = _scan()
    dt = max(0.05, now - _pp_t)
    rows = []
    for pid, (comm, ticks, rss) in cur.items():
        pct = max(0.0, (ticks - _pp.get(pid, ticks)) / clk / dt * 100)
        rows.append((pid, comm, pct, rss))
    _pp = {p: v[1] for p, v in cur.items()}
    _pp_t = now

    def row(r):
        pid, comm, pct, rss = r
        try:
            user = _user(os.stat(f"/proc/{pid}").st_uid)
        except OSError:
            user = "?"
        return {"pid": pid, "name": _pname(pid, comm), "user": user, "cpu": round(pct, 1), "rss": rss}
    return {"cpu": [row(r) for r in sorted(rows, key=lambda r: -r[2])[:8]],
            "mem": [row(r) for r in sorted(rows, key=lambda r: -r[3])[:8]]}


# ---------- small network helpers ----------

def _tcp_ms(host, port, timeout=3):
    t = time.monotonic()
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout):
            pass
        return max(1, int((time.monotonic() - t) * 1000))
    except (OSError, ValueError):
        return None


def _systemctl_active(unit):
    exe = shutil.which("systemctl")
    if not exe or not unit:
        return None
    try:
        return subprocess.run([exe, "is-active", unit], capture_output=True, text=True, timeout=2).stdout.strip() or None
    except Exception:
        return None


def _proc_arg(comm, flags):
    """Value following one of `flags` on the command line of the running process called `comm`."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() != comm:
                    continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = [a.decode("utf-8", "replace") for a in f.read().split(b"\0")]
        except OSError:
            continue
        for i, a in enumerate(args[:-1]):
            if a in flags:
                return args[i + 1]
        for a in args:  # -c=path / --config=path
            for fl in flags:
                if a.startswith(fl + "="):
                    return a[len(fl) + 1:]
    return None


def _pool_map(fn, items, workers=6):
    with ThreadPoolExecutor(workers) as p:
        return list(p.map(fn, items))


# ---------- frp ----------

def _parse_frpc(path):
    """serverAddr/serverPort/protocol + proxies (name, type, ports). Never reads tokens into the result."""
    with open(path, "rb") as f:
        txt = f.read().decode("utf-8", "replace")
    try:
        import tomllib
        c = tomllib.loads(txt)
        tr = c.get("transport") or {}
        return {"server": c.get("serverAddr") or c.get("server_addr"),
                "port": int(c.get("serverPort") or c.get("server_port") or 7000),
                "proto": (tr.get("protocol") or "tcp") if isinstance(tr, dict) else "tcp",
                "proxies": [{"name": p.get("name"), "type": p.get("type", "tcp"), "local": p.get("localPort"),
                             "remote": p.get("remotePort"),
                             "domains": p.get("customDomains") or ([p["subdomain"]] if p.get("subdomain") else [])}
                            for p in c.get("proxies", []) if isinstance(p, dict)]}
    except ImportError:  # Python < 3.11: tiny regex reader, good enough for the keys we need
        pass
    pick = lambda blk, k: (re.search(r'^\s*%s\s*=\s*"?([^"\n#]+)"?' % re.escape(k), blk, re.M) or [None, None])[1]
    head, *blocks = re.split(r"^\s*\[\[proxies\]\]", txt, flags=re.M)
    port = (pick(head, "serverPort") or "").strip()
    px = []
    for blk in blocks:
        num = lambda k: int(pick(blk, k)) if (pick(blk, k) or "").strip().isdigit() else None
        px.append({"name": (pick(blk, "name") or "").strip(), "type": (pick(blk, "type") or "tcp").strip(),
                   "local": num("localPort"), "remote": num("remotePort"), "domains": []})
    return {"server": (pick(head, "serverAddr") or "").strip() or None, "port": int(port) if port.isdigit() else 7000,
            "proto": (pick(head, "transport.protocol") or "tcp").strip(), "proxies": px}


def frp():
    path = _get("FRP_CONFIG") or _proc_arg("frpc", ("-c", "--config")) or "/etc/frp/frpc.toml"
    unit = _get("FRP_SERVICE", "frp")
    active = _systemctl_active(unit)
    running = active == "active" or _proc_arg("frpc", ("-c", "--config")) is not None
    if active is None and not running and not os.path.exists(path):
        return None  # frp isn't on this machine
    try:
        c = _parse_frpc(path)
    except Exception:
        c = {"server": None, "port": None, "proto": None, "proxies": []}
    server, proxies = c["server"], c["proxies"]
    res = {"active": active or ("active" if running else "unknown"), "server": server, "port": c["port"],
           "proto": c["proto"], "ping": None, "proxies": []}
    checked = memo("frp_proxies", 60, lambda: _pool_map(  # remote ports: once a minute so sshd etc. isn't hammered
        lambda p: _tcp_ms(server, p["remote"]) if server and p.get("remote") else None, proxies)) or [None] * len(proxies)
    for p, ms in zip(proxies, checked):
        res["proxies"].append({**p, "up": ms is not None if p.get("remote") else None, "ms": ms})
    if server and c["proto"] in ("tcp", "websocket", "wss", None):
        res["ping"] = _tcp_ms(server, c["port"])  # control port is TCP: ping it directly
    else:  # quic/kcp run over UDP: use the round trip to the forwarded ports (it goes through the tunnel host)
        ms = [p["ms"] for p in res["proxies"] if p["ms"] is not None]
        res["ping"] = min(ms) if ms else None
    return res


# ---------- xray ----------

def _xray_conf():
    path = _get("XRAY_CONFIG") or _proc_arg("xray", ("-config", "-c", "--config")) or "/usr/local/etc/xray/config.json"
    with open(path, encoding="utf-8") as f:
        c = json.load(f)
    inb = [{"protocol": i.get("protocol"), "port": i.get("port"), "listen": i.get("listen")} for i in c.get("inbounds", [])]
    outb = []
    for o in c.get("outbounds", []):
        s = o.get("settings") or {}
        node = (s.get("vnext") or s.get("servers") or [{}])[0]
        if node.get("address") and o.get("protocol") not in ("freedom", "blackhole", "dns"):
            outb.append({"tag": o.get("tag"), "protocol": o.get("protocol"), "address": node["address"], "port": node.get("port")})
    return inb, outb


def _via_proxy(proxy, url, timeout=6, read=0):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    t = time.monotonic()
    with op.open(urllib.request.Request(url, headers={"User-Agent": "watchcat"}), timeout=timeout) as r:
        body = r.read(read) if read else b""
        return int((time.monotonic() - t) * 1000), r.status, body


def xray():
    unit = _get("XRAY_SERVICE", "xray")
    active = _systemctl_active(unit)
    try:
        inb, outb = _xray_conf()
    except Exception:
        inb, outb = [], []
    if active is None and not inb and not _get("XRAY_PROXY"):
        return None
    proxy = _get("XRAY_PROXY")
    if not proxy:
        http = next((i for i in inb if i["protocol"] == "http" and i["port"]), None)
        if http:
            host = http["listen"] if http["listen"] not in (None, "", "0.0.0.0", "::") else "127.0.0.1"
            proxy = f"http://{host}:{http['port']}"
    res = {"active": active or "unknown", "proxy": proxy, "ping": None, "ok": None, "exit_ip": None,
           "inbounds": inb, "outbounds": [], "name": None, "name_kind": None}
    if proxy:
        try:
            ms, status, _ = _via_proxy(proxy, _get("XRAY_TEST_URL", "https://www.gstatic.com/generate_204"))
            res["ping"], res["ok"] = ms, status in (200, 204)
        except Exception:
            res["ok"] = False

        def exit_ip():
            _, _, body = _via_proxy(proxy, _get("XRAY_IP_URL", "https://api.ipify.org"), read=64)
            return str(ipaddress.ip_address(body.decode().strip()))
        if res["ok"]:
            res["exit_ip"] = memo("xray_ip", 120, exit_ip)
    def probe(o):
        try:
            ips = {a[4][0] for a in socket.getaddrinfo(o["address"], None)}
        except OSError:
            ips = set()
        return {**o, "ms": _tcp_ms(o["address"], o["port"]), "ips": sorted(ips)}
    res["outbounds"] = memo("xray_out", 30, lambda: _pool_map(probe, outb)) or []
    for o in res["outbounds"]:
        o["active"] = bool(res["exit_ip"] and res["exit_ip"] in o.get("ips", []))
    cand = [o for o in res["outbounds"] if o["active"]]
    if cand:
        res["name"], res["name_kind"] = " / ".join(o["tag"] or "?" for o in cand), "exit"
    else:
        up = [o for o in res["outbounds"] if o["ms"] is not None]
        if up:  # balancer picks the lowest ping; the exit IP didn't match any entry address
            res["name"], res["name_kind"] = min(up, key=lambda o: o["ms"])["tag"], "fastest"
    return res


# ---------- custom command tiles ("watch -n N") ----------

CMD_CAP = 16 * 1024
_cmd_res = {}
_cmd_running = set()
_cmd_pool = ThreadPoolExecutor(4, thread_name_prefix="cmd")
_viewer = 0.0
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]")


def run_capped(cmd, timeout):
    """Run `sh -c cmd` with a hard time limit and output cap; whole process group is killed on overrun."""
    env = {k: v for k, v in os.environ.items() if k not in ("PASSWORD", "WATCHCAT_PASSWORD")}
    env.update(TERM="dumb", COLUMNS="140", LINES="50", LC_ALL=env.get("LC_ALL", "C.UTF-8"))
    p = subprocess.Popen(["/bin/sh", "-c", cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         env=env, start_new_session=True)
    buf, fd, end, why = bytearray(), p.stdout.fileno(), time.monotonic() + timeout, None
    while True:
        left = end - time.monotonic()
        if left <= 0:
            why = "timeout"
            break
        if select.select([fd], [], [], min(left, 0.25))[0]:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > CMD_CAP:
                why = "truncated"
                break
        elif p.poll() is not None:
            break
    if p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    p.wait()
    p.stdout.close()
    text = _ANSI.sub("", bytes(buf[:CMD_CAP]).decode("utf-8", "replace")).replace("\r\n", "\n").replace("\r", "\n")
    return text, (None if why == "timeout" else p.returncode), why


def _run_cmd(c):
    t = time.monotonic()
    try:
        out, rc, why = run_capped(c["cmd"], min(max(c["interval"] * 2, 5), 15))
    except Exception as e:
        out, rc, why = str(e), None, "error"
    _cmd_res[c["id"]] = {"out": out, "rc": rc, "why": why, "ms": int((time.monotonic() - t) * 1000),
                         "ts": time.time(), "started": t}
    _cmd_running.discard(c["id"])


def _cmd_loop():
    """Re-run each configured command every `interval` seconds - but only while someone is looking."""
    while True:
        time.sleep(0.5)
        if time.monotonic() - _viewer > 15 or not conf.get("cmds_enabled"):
            continue
        now = time.monotonic()
        ids = {c["id"] for c in conf["cmds"]}
        for k in [k for k in _cmd_res if k not in ids]:
            del _cmd_res[k]
        for c in conf["cmds"]:
            r = _cmd_res.get(c["id"])
            if c["id"] in _cmd_running or (r and now - r["started"] < c["interval"]):
                continue
            _cmd_running.add(c["id"])
            _cmd_pool.submit(_run_cmd, c)


# ---------- everything for /api/sys ----------

def collect(full):
    """full=False -> redacted view for visitors who are not logged in."""
    global _viewer, _budget_end
    _viewer = time.monotonic()
    hidden = set(conf["hidden"])
    # start every slow collector first so a cold start waits for the slowest one, not for the sum
    kicks = {"gpu": gpu, "ollama": ollama, "drives": drives, "docker": docker, "frp": frp, "xray": xray}
    ttls = {"gpu": 2, "ollama": 3, "drives": 15, "docker": 5, "frp": 10, "xray": 10}
    for k, fn in kicks.items():
        bg(k, ttls[k], fn, wait=False)
    if full:
        bg("procs", 2, procs, wait=False)
    _budget_end = time.monotonic() + 1.5
    tiles, locked = {}, []
    put = lambda k, v: tiles.__setitem__(k, v) if v is not None and k not in hidden else None

    if "cpu" not in hidden:
        put("cpu", cpu())
    if "ram" not in hidden:
        put("ram", ram())
    g = bg("gpu", 2, gpu) if ({"gpu", "vram"} - hidden) else None
    put("gpu", g)
    put("vram", g)
    put("ollama", bg("ollama", 3, ollama) if "ollama" not in hidden else None)
    if "drives" not in hidden:
        hide = set(DEFAULT_HIDE_MOUNTS) | set(conf["hide_mounts"])
        d = bg("drives", 15, drives)
        put("drives", [x for x in d if x["mount"] not in hide] if d is not None else None)
        tiles["_mounts"] = [x["mount"] for x in d or []]
    if "docker" not in hidden:
        d = bg("docker", 5, docker)
        put("docker", (d if full else {"count": len(d), "redacted": True}) if d is not None else None)
        if d is not None and not full:
            locked.append("Docker details")
    if {"procs_cpu", "procs_mem"} - hidden:
        if full:
            pr = bg("procs", 2, procs)
            put("procs_cpu", pr["cpu"] if pr else None)
            put("procs_mem", pr["mem"] if pr else None)
        else:
            locked.append("processes")
    if "frp" not in hidden:
        f = bg("frp", 10, frp)
        if f is not None and not full:
            f = {"active": f["active"], "ping": f["ping"], "redacted": True,
                 "n": len(f["proxies"]), "up": sum(1 for p in f["proxies"] if p["up"])}
            locked.append("frp ports")
        put("frp", f)
    if "xray" not in hidden:
        x = bg("xray", 10, xray)
        if x is not None and not full:
            x = {"active": x["active"], "ping": x["ping"], "ok": x["ok"], "redacted": True}
            locked.append("xray details")
        put("xray", x)
    cmds = []
    if full and conf.get("cmds_enabled"):
        for c in conf["cmds"]:
            r = _cmd_res.get(c["id"])
            cmds.append({"id": c["id"], "name": c["name"], "wide": c.get("wide", True), "interval": c["interval"],
                         **({k: r[k] for k in ("out", "rc", "why", "ms", "ts")} if r else {})})
    elif not full and conf["cmds"]:
        locked.append("command tiles")
    return {"supported": SUPPORTED, "t": time.time(), "tiles": tiles, "cmds": cmds, "locked": locked}
