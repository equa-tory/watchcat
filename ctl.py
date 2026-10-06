"""Actions behind the Server page's buttons. A fixed whitelist - nothing here ever takes a command line
from a request:

  * Ollama: unload one model / all models (Ollama's own API, no root needed)
  * xray:   start / stop / restart its systemd unit
  * frp:    restart its unit, and switch individual [[proxies]] on/off in frpc.toml (then restart)
  * drives: re-read a disk's size and grow a filesystem into unallocated space (alloc.py decides the commands)

systemd actions go through `sudo -n /usr/bin/systemctl <verb> <unit>`; they only work for the exact
command lines allowed by /etc/sudoers.d/watchcat (see tools/install-sudoers.sh). Standard library only.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import alloc
import sysinfo

SUDO, SYSTEMCTL = "/usr/bin/sudo", "/usr/bin/systemctl"
_lock = threading.Lock()
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CtlError(Exception):
    def __init__(self, msg, code=500):
        super().__init__(msg)
        self.msg, self.code = msg, code


def unit(env, default):
    name = sysinfo._get(env, default).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", name):
        raise CtlError("Invalid unit name in " + env)
    return name if name.endswith(".service") else name + ".service"


def units():
    return {"xray": unit("XRAY_SERVICE", "xray"), "frp": unit("FRP_SERVICE", "frp")}


def log(msg):
    sys.stderr.write("ctl: " + msg + "\n")


def _dry():
    return os.environ.get("CTL_DRY_RUN") == "1"  # for tests: log instead of touching systemd


def systemctl(verb, u):
    if verb not in ("start", "stop", "restart"):
        raise CtlError("Bad action", 400)
    log(f"systemctl {verb} {u}")
    if _dry():
        return
    try:
        r = subprocess.run([SUDO, "-n", SYSTEMCTL, verb, u], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CtlError(f"Could not run systemctl: {e}")
    if r.returncode:
        out = (r.stderr or r.stdout).strip()
        if "password is required" in out or "not allowed" in out or "may not run" in out:
            raise CtlError(f"Not allowed to {verb} {u}: install the sudoers file once (see Settings -> Server page).", 403)
        raise CtlError(f"systemctl {verb} {u} failed: {out[:200]}")


def _nopasswd_commands(listing):
    """Commands sudo will run for us WITHOUT a password, from `sudo -l` output (plain `sudo -l <cmd>` also
    succeeds for commands that need a password, so it can't be used as the test)."""
    entries = []
    for line in listing.splitlines():
        if re.match(r"\s*\(", line):
            entries.append(line.strip())
        elif entries and line.startswith(" ") and line.strip():
            entries[-1] += " " + line.strip()  # wrapped continuation
    allowed, all_ok = set(), False
    for e in entries:
        m = re.match(r"\(([^)]*)\)\s*(.*)", e)
        if not m or not re.search(r"\b(root|ALL)\b", m.group(1)):
            continue
        nopass = False
        for tok in m.group(2).split(","):
            tok = tok.strip()
            while True:
                t = re.match(r"([A-Z_]+):\s*(.*)", tok)
                if not t:
                    break
                if t.group(1) == "NOPASSWD":
                    nopass = True
                elif t.group(1) == "PASSWD":
                    nopass = False
                tok = t.group(2)
            if nopass and tok == "ALL":
                all_ok = True
            elif nopass and tok:
                allowed.add(tok)
    return allowed, all_ok


def caps():
    """Which buttons can work: Ollama via its API, systemd actions only if sudo allows that exact command."""
    out = {"ollama": True, "xray": False, "frp": False, "alloc": False}
    u = units()
    if _dry():
        return {"ollama": True, "xray": True, "frp": True, "alloc": True}
    try:
        r = subprocess.run([SUDO, "-n", "-l"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return out
    if r.returncode:
        return out
    allowed, all_ok = _nopasswd_commands(r.stdout)
    can = lambda verb, name: all_ok or f"{SYSTEMCTL} {verb} {name}" in allowed
    out["xray"] = all(can(v, u["xray"]) for v in ("start", "stop", "restart"))
    out["frp"] = can("restart", u["frp"])
    unesc = lambda c: re.sub(r"\\(.)", r"\1", c)
    have = {unesc(c) for c in allowed}
    out["alloc"] = all_ok or all(unesc(c) in have for c in alloc.sudoers_commands())   # nothing growable -> trivially true
    return out


# ---------- Ollama ----------

def _ollama_base():
    base = sysinfo._get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    return base if "://" in base else "http://" + base


def _ollama_json(path, body=None, timeout=15):
    req = urllib.request.Request(_ollama_base() + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with _direct.open(req, timeout=timeout) as r:
        return json.loads(r.read(1_000_000) or b"{}")


def ollama_unload(model=None):
    """Unload one model, or every loaded model when model is None -> frees its VRAM."""
    try:
        loaded = [m["name"] for m in _ollama_json("/api/ps").get("models", [])]
    except Exception as e:
        raise CtlError(f"Ollama isn't reachable: {e}")
    targets = loaded if model is None else [model]
    if model is not None and model not in loaded:
        raise CtlError("That model isn't loaded", 404)
    for name in targets:
        log(f"ollama unload {name}")
        _ollama_json("/api/generate", {"model": name, "keep_alive": 0})
    sysinfo.refresh("ollama", "gpu")
    return len(targets)


# ---------- frp: switch proxies on/off in frpc.toml ----------

def _apply_toggles(text, enabled):
    """Comment out / restore whole [[proxies]] blocks (every line gets the reversible FRP_OFF prefix)."""
    lines = text.split("\n")
    changed = []
    for blk in sysinfo.frp_blocks(text):
        want = enabled.get(blk["name"])
        if want is None or want == (not blk["off"]):
            continue
        for i in range(blk["start"], blk["end"]):
            if want:
                if lines[i].startswith(sysinfo.FRP_OFF):
                    lines[i] = lines[i][len(sysinfo.FRP_OFF):]
            elif lines[i].strip():
                lines[i] = sysinfo.FRP_OFF + lines[i]
        changed.append(blk["name"])
    return "\n".join(lines), changed


def frp_set_proxies(enabled, restart=True):
    """enabled: {proxy name: bool}. Edits frpc.toml (validated + backed up) and restarts frp."""
    path = sysinfo.frp_config_path()
    if not os.path.isfile(path):
        raise CtlError("frpc config not found: " + path, 404)
    names = {b["name"] for b in sysinfo.frp_blocks(open(path, encoding="utf-8").read())}
    unknown = [n for n in enabled if n not in names]
    if unknown:
        raise CtlError("Unknown proxy: " + ", ".join(unknown), 400)
    with _lock:
        text = open(path, encoding="utf-8").read()
        new, changed = _apply_toggles(text, enabled)
        if not changed:
            return []
        try:
            import tomllib
            tomllib.loads(new)  # never write a file frpc can't parse
        except ImportError:
            pass
        except Exception as e:
            raise CtlError("Refusing to write an invalid frpc.toml: " + str(e)[:120])
        shutil.copy2(path, path + ".watchcat.bak")
        tmp = path + ".watchcat.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
        log("frp proxies " + ", ".join(f"{n}={'on' if enabled[n] else 'off'}" for n in changed))
    if restart:
        systemctl("restart", units()["frp"])
    sysinfo.refresh("frp")
    return changed


def frp_restart():
    systemctl("restart", units()["frp"])
    sysinfo.refresh("frp")


def xray_action(verb):
    systemctl(verb, units()["xray"])
    time.sleep(0.5)
    sysinfo.refresh("xray", "frp")


# ---------- drives: unallocated space ----------

def _sudo(argv, stdin=None, timeout=600):
    log("sudo " + " ".join(argv))
    if _dry():
        return 0, "(dry run)"
    try:
        r = subprocess.run([SUDO, "-n"] + argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CtlError(f"Could not run {os.path.basename(argv[0])}: {e}")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode and ("password is required" in out or "not allowed" in out or "may not run" in out):
        raise CtlError("Not allowed to run " + os.path.basename(argv[0]) + ": re-run tools/install-sudoers.sh once "
                       "(it adds the disk commands for the current drives).", 403)
    return r.returncode, out


def _layout(mount):
    for lay in alloc.layouts():       # only mounts we found ourselves: a request can't name its own device
        if lay.mount == mount:
            return lay
    raise CtlError("That drive can't be resized from here", 404)


def alloc_check(mount):
    """Ask the kernel to re-read the disk size (a VM disk enlarged in the hypervisor isn't seen until then), then
    report how much room the filesystem could gain."""
    lay = _layout(mount)
    note = ""
    with _lock:
        for disk in lay.disks():
            p = alloc.rescan_path(disk)
            if not p:
                continue
            try:
                rc, out = _sudo([alloc.exe("tee"), p], stdin="1\n", timeout=15)
                if rc:
                    note = " (disk rescan failed)"
            except CtlError as e:
                note = " (disk not rescanned: sudo rule missing)" if e.code == 403 else " (disk rescan failed)"
        time.sleep(0.4)
    sysinfo.refresh("drives")
    lay = _layout(mount)
    free = lay.free()
    if free < alloc.min_bytes():
        return 0, f"{mount}: nothing unallocated{note}"
    return free, f"{mount}: {sysinfo.fmt_bytes(free)} unallocated{note}"


def alloc_grow(mount):
    """Use ALL the unallocated space: growpart -> pvresize -> lvextend/resize2fs, each step an exact whitelisted
    command. Every step only ever grows something; re-running after a failure simply continues."""
    with _lock:
        lay = _layout(mount)
        free = lay.free()
        if free < alloc.min_bytes():
            raise CtlError(f"{mount}: nothing unallocated", 409)
        steps = lay.steps()
        done = []
        for argv in steps:
            name = os.path.basename(argv[0])
            rc, out = _sudo(argv)
            if rc and name == "growpart" and "NOCHANGE" in out:
                rc = 0
            if rc:
                raise CtlError(f"{name} failed" + (f" after {', '.join(done)}" if done else "") + ": " + out[-240:])
            done.append(name)
    sysinfo.refresh("drives")
    return free, f"{mount}: grown by {sysinfo.fmt_bytes(free)} ({' → '.join(done)})"
