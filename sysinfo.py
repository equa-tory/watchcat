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

import alloc

SUPPORTED = sys.platform.startswith("linux")
_get = lambda key, default="": os.environ.get(key, default)  # replaced by init() with server.cfg
_lock = threading.Lock()

# tile ids the user can hide
BUILTIN = ["cpu", "ram", "gpu", "vram", "ollama", "drives", "dirs", "docker", "procs_cpu", "procs_mem", "frp", "xray"]
conf = {"hidden": [], "hide_mounts": [], "cmds": []}  # set by server via configure()
DEFAULT_HIDE_MOUNTS = ["/boot", "/boot/efi"]


_data_dir = None


def init(getter, data_dir=None):
    global _get, _data_dir
    _get = getter
    alloc.GET = getter
    _data_dir = data_dir
    if SUPPORTED:
        _load_state()
        threading.Thread(target=_cmd_loop, daemon=True).start()
        if float(_get("SYS_SAMPLE_SECONDS", str(HIST_STEP)) or 0) > 0:
            threading.Thread(target=_sampler, daemon=True).start()
        threading.Thread(target=_du_loop, daemon=True).start()


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


def fmt_bytes(b):
    return f"{b / 2**40:.1f} TB" if b >= 2**40 else f"{b / 2**30:.1f} GB" if b >= 2**30 else f"{b / 2**20:.0f} MB"


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
        d = {"mount": mp, "fstype": fs, "size": size, "used": used, "avail": free,
             "pct": round(100 * used / max(1, used + free), 1)}
        try:
            a = alloc.inspect(dev, mp, fs) if dev.startswith("/dev/") else None   # None: can't be grown from here
        except Exception:
            a = None
        if a is not None:
            d["alloc"] = a
        out.append(d)
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

FRP_OFF = "#watchcat-off# "   # prefix watchcat puts on every line of a proxy it has switched off (fully reversible)


def frp_blocks(text):
    """Locate every [[proxies]] block (enabled or switched off by watchcat): name, type, ports, line range."""
    lines = text.split("\n")
    un = lambda l: l[len(FRP_OFF):] if l.startswith(FRP_OFF) else l
    blocks, cur = [], None
    for i, raw in enumerate(lines):
        l = un(raw)
        if re.match(r"\s*\[\[\s*proxies\s*\]\]", l):
            cur = {"start": i, "end": i + 1, "off": raw.startswith(FRP_OFF)}
            blocks.append(cur)
        elif re.match(r"\s*\[", l):
            cur = None  # some other table ends the block
        elif cur is not None and l.strip() and not l.lstrip().startswith("#"):
            cur["end"] = i + 1   # block ends at its last real setting, not at trailing comments
    for blk in blocks:
        body = "\n".join(un(x) for x in lines[blk["start"]:blk["end"]])
        g = lambda k: (re.search(r'^\s*%s\s*=\s*"?([^"\n#]+)"?' % k, body, re.M) or [None, None])[1]
        num = lambda k: int(g(k)) if (g(k) or "").strip().isdigit() else None
        blk.update(name=(g("name") or "").strip(), type=(g("type") or "tcp").strip(), local=num("localPort"), remote=num("remotePort"))
    return blocks


def _parse_frpc(path):
    """serverAddr/serverPort/protocol/proxyURL + proxies (name, type, ports, enabled). Never reads tokens into the result."""
    with open(path, "rb") as f:
        txt = f.read().decode("utf-8", "replace")
    pick = lambda blk, k: (re.search(r'^\s*%s\s*=\s*"?([^"\n#]+)"?' % re.escape(k), blk, re.M) or [None, None])[1]
    head = re.split(r"^\s*\[\[proxies\]\]", txt, maxsplit=1, flags=re.M)[0]
    server, port, proto, purl = None, 7000, "tcp", None
    try:
        import tomllib
        c = tomllib.loads(txt)
        tr = c.get("transport") if isinstance(c.get("transport"), dict) else {}
        server, port = c.get("serverAddr") or c.get("server_addr"), int(c.get("serverPort") or c.get("server_port") or 7000)
        proto, purl = tr.get("protocol") or "tcp", tr.get("proxyURL")
    except ImportError:  # Python < 3.11: regex reader is good enough for the keys we need
        server = (pick(head, "serverAddr") or "").strip() or None
        p = (pick(head, "serverPort") or "").strip()
        port = int(p) if p.isdigit() else 7000
        proto, purl = (pick(head, "transport.protocol") or "tcp").strip(), (pick(head, "transport.proxyURL") or "").strip() or None
    proxies = [{"name": b["name"], "type": b["type"], "local": b["local"], "remote": b["remote"], "enabled": not b["off"], "domains": []}
               for b in frp_blocks(txt)]
    return {"server": server, "port": port, "proto": proto, "proxy_url": purl, "proxies": proxies}


def frp_config_path():
    return _get("FRP_CONFIG") or _proc_arg("frpc", ("-c", "--config")) or "/etc/frp/frpc.toml"


def refresh(*keys):
    """Make the next /api/sys recompute these sources (after a control action changed something)."""
    for k in keys:
        if k in _slots:
            _slots[k].ts = -1e9
        for mk in [m for m in _memo if m.startswith(k)]:
            del _memo[mk]


def frp():
    path = frp_config_path()
    unit = _get("FRP_SERVICE", "frp")
    active = _systemctl_active(unit)
    running = active == "active" or _proc_arg("frpc", ("-c", "--config")) is not None
    if active is None and not running and not os.path.exists(path):
        return None  # frp isn't on this machine
    try:
        c = _parse_frpc(path)
    except Exception:
        c = {"server": None, "port": None, "proto": None, "proxy_url": None, "proxies": []}
    server, proxies = c["server"], c["proxies"]
    res = {"active": active or ("active" if running else "unknown"), "server": server, "port": c["port"],
           "proto": c["proto"], "via_proxy": bool(c["proxy_url"]), "ping": None, "proxies": []}
    live = [p for p in proxies if p["enabled"]]
    checked = memo("frp_proxies", 60, lambda: _pool_map(  # remote ports: once a minute so sshd etc. isn't hammered
        lambda p: _tcp_ms(server, p["remote"]) if server and p.get("remote") else None, live)) or [None] * len(live)
    ms_of = {id(p): ms for p, ms in zip(live, checked)}
    for p in proxies:
        ms = ms_of.get(id(p))
        res["proxies"].append({**p, "up": (ms is not None if p.get("remote") else None) if p["enabled"] else None, "ms": ms})
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


# ---------- last-hour history for sparklines ----------
# A background sampler keeps ~1 hour of CPU / RAM / GPU values (every 15 s) so a tile can show whether
# a number is a spike or sustained. A few hundred floats in memory; saved every 5 min so a restart keeps it.

HIST_STEP = 15
HIST_N = 240
_samples = []  # [(ts, {series: value})] oldest first


def _sample_once(prev_cpu):
    pt, cur = {}, _cpu_sample()
    if prev_cpu and cur[0] > prev_cpu[0]:
        pt["cpu"] = round(100 * (1 - (cur[1] - prev_cpu[1]) / (cur[0] - prev_cpu[0])), 1)
    try:
        r = ram()
        pt["ram"] = round(100 * r["used"] / r["total"], 1)
    except Exception:
        pass
    try:
        g = gpu()
        if g:
            pt["gpu_temp"], pt["gpu_util"] = g[0]["temp"], g[0]["util"]
            if g[0]["mem_total"]:
                pt["vram"] = round(100 * g[0]["mem_used"] / g[0]["mem_total"], 1)
    except Exception:
        pass
    return cur, {k: v for k, v in pt.items() if v is not None}


def _save_samples():
    if not _data_dir:
        return
    try:
        with _lock:
            snap = list(_samples)
        tmp = os.path.join(_data_dir, "metrics.json.tmp")
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, os.path.join(_data_dir, "metrics.json"))
    except OSError:
        pass


def _sampler():
    step = max(5.0, float(_get("SYS_SAMPLE_SECONDS", str(HIST_STEP)) or HIST_STEP))
    prev, last_save = None, time.monotonic()
    while True:
        t0 = time.monotonic()
        try:
            prev, pt = _sample_once(prev)
        except Exception:
            pt = {}
        if pt:
            with _lock:
                _samples.append((int(time.time()), pt))
                del _samples[:-HIST_N]
        if t0 - last_save > 300:
            _save_samples()
            last_save = t0
        time.sleep(max(1.0, step - (time.monotonic() - t0)))


def history():
    with _lock:
        samples = list(_samples)
    keys = sorted({k for _, p in samples for k in p})
    return {"now": time.time(), "step": HIST_STEP, "t": [t for t, _ in samples],
            "s": {k: [p.get(k) for _, p in samples] for k in keys}}


# ---------- folder sizes on each drive (like `du -sh /mnt/x/*`) ----------
# Walking a big disk is heavy I/O, so: one folder at a time, `nice` + lowest `ionice` priority, local disks only
# (network shares are skipped unless DU_NETWORK=1), refreshed every DU_INTERVAL_HOURS (default 6, 0 = off)
# and cached on disk so a restart doesn't rescan.

NET_FS = {"cifs", "smb3", "nfs", "nfs4", "davfs", "fuse.sshfs"}
_du = {}  # mount -> {"ts", "items": [{"name", "size", "dir"}]}


def _load_state():
    if not _data_dir:
        return
    now = time.time()
    try:
        with open(os.path.join(_data_dir, "metrics.json")) as f:
            for ts, pt in json.load(f):
                if isinstance(ts, (int, float)) and now - ts < HIST_STEP * HIST_N and isinstance(pt, dict):
                    _samples.append((int(ts), pt))
    except (OSError, ValueError, TypeError):
        pass
    try:
        with open(os.path.join(_data_dir, "dirsizes.json")) as f:
            d = json.load(f)
        if isinstance(d, dict):
            _du.update({k: v for k, v in d.items() if isinstance(v, dict) and isinstance(v.get("items"), list)})
    except (OSError, ValueError):
        pass


def _local_mounts(include_net):
    seen, out = set(), []
    with open("/proc/self/mounts") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 3:
                continue
            dev, mp, fs = parts[0], _unescape(parts[1]), parts[2]
            if fs not in REAL_FS or dev in seen or mp.startswith(SKIP_PREFIX) or (fs in NET_FS and not include_net):
                continue
            seen.add(dev)
            out.append(mp)
    return out


def _du_scan_mount(mp):
    import stat as _stat
    du = shutil.which("du")
    if not du:
        return None
    prefix = (["nice", "-n", "19"] if shutil.which("nice") else []) + (["ionice", "-c", "2", "-n", "7"] if shutil.which("ionice") else [])
    try:
        names = os.listdir(mp)
    except OSError:
        return None
    items = []
    for n in names:
        p = os.path.join(mp, n)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if not _stat.S_ISDIR(st.st_mode):
            items.append({"name": n, "size": st.st_blocks * 512, "dir": False})
        elif not os.path.ismount(p):  # a different filesystem isn't part of this drive
            size = None
            try:
                r = subprocess.run(prefix + [du, "-sx", "--block-size=1", "--", p], capture_output=True, text=True, timeout=1800)
                size = int(r.stdout.split()[0]) if r.stdout.strip() else None  # exit 1 = some unreadable files; total still printed
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
            items.append({"name": n, "size": size, "dir": True})
    items = [i for i in items if i["size"] is not None]
    items.sort(key=lambda i: -i["size"])
    return items[:20]


def _du_loop():
    time.sleep(90)
    while True:
        try:
            hours = float(_get("DU_INTERVAL_HOURS", "6") or 0)
        except ValueError:
            hours = 6.0
        if hours > 0 and "dirs" not in conf["hidden"]:
            skip = set(DEFAULT_HIDE_MOUNTS) | set(conf["hide_mounts"])
            for mp in _local_mounts(_get("DU_NETWORK") == "1"):
                if mp in skip or time.time() - _du.get(mp, {}).get("ts", 0) < hours * 3600:
                    continue
                items = _du_scan_mount(mp)
                if items is not None:
                    with _lock:
                        _du[mp] = {"ts": time.time(), "items": items}
                    if _data_dir:
                        try:
                            tmp = os.path.join(_data_dir, "dirsizes.json.tmp")
                            with open(tmp, "w") as f:
                                json.dump(_du, f)
                            os.replace(tmp, os.path.join(_data_dir, "dirsizes.json"))
                        except OSError:
                            pass
        time.sleep(600)


# ---------- everything for /api/sys ----------

def collect(full, peek=False):
    """full=False -> redacted view for visitors who are not logged in.
    peek=True only warms the caches (page just loaded): it must not wake the command tiles."""
    global _viewer, _budget_end
    if not peek:
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
        with_dirs = "dirs" not in hidden
        put("drives", [({**x, "dirs": _du.get(x["mount"])} if with_dirs and x["mount"] in _du else x)
                       for x in d if x["mount"] not in hide] if d is not None else None)
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
                 "n": sum(1 for p in f["proxies"] if p["enabled"]), "up": sum(1 for p in f["proxies"] if p["up"])}
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
