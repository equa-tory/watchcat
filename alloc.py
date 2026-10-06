"""Unallocated disk space: find it (no root needed, sysfs only) and describe the exact commands that would use it.

Two layouts are understood, both on ext2/3/4 or xfs:
  * filesystem on a partition      -> growpart <disk> <n>, then resize2fs / xfs_growfs
  * filesystem on an LVM volume    -> growpart (if the disk grew), pvresize, lvextend -l +100%FREE -r
"Unallocated" is room that exists but isn't used yet: empty space on the disk after the partition, or free
extents in the volume group. Nothing here ever shrinks, moves or deletes anything. Standard library only.

  python3 alloc.py            # human-readable report
  python3 alloc.py --sudoers  # the exact sudo command lines the installer should allow (one per line)
"""
import os
import re
import shutil
import sys

GET = os.environ.get                         # sysinfo.init() swaps in watchcat's .env-aware getter
SYS = os.environ.get("WC_SYSFS", "/sys")      # overridable so tests can use a fake tree
GROWABLE_FS = {"ext2", "ext3", "ext4", "xfs"}
PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MiB = 1 << 20


def min_bytes():
    try:
        return max(1, int(float(GET("ALLOC_MIN_MB", "128") or 128))) * MiB
    except ValueError:
        return 128 * MiB


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _int(path):
    v = _read(path)
    return int(v) if v and v.isdigit() else None


def exe(name):
    return shutil.which(name, path=PATH) or "/usr/sbin/" + name


def esc(arg):
    """Escape an argument the way sudoers wants it (so the line is an exact match, never a pattern)."""
    return re.sub(r"([\\,:= \t])", r"\\\1", arg)


class Layout:
    """What backs one mounted filesystem, as read from sysfs."""

    def __init__(self, dev, mount, fstype):
        self.mount, self.fstype = mount, fstype
        self.kind = None          # "part" | "lvm"
        self.why = None           # reason it can't be handled
        self.parts = []           # [(disk, partnum or None, node)]  the physical devices to grow
        self.lv = None            # "/dev/vg/lv"
        self.node = None          # device node holding the filesystem
        self.pv_total = self.lv_total = 0
        try:
            self._probe(dev)
        except (OSError, ValueError, IndexError):
            self.why = "layout not readable"

    # -- helpers on /sys
    @staticmethod
    def _name(dev):
        return os.path.basename(os.path.realpath(dev))

    def _is_part(self, n):
        return os.path.exists(f"{SYS}/class/block/{n}/partition")

    def _disk_of(self, n):
        return os.path.basename(os.path.dirname(os.path.realpath(f"{SYS}/class/block/{n}")))

    def _probe(self, dev):
        n = self._name(dev)
        if not os.path.isdir(f"{SYS}/class/block/{n}"):
            self.why = "not a block device"
            return
        if self.fstype not in GROWABLE_FS:
            self.why = f"{self.fstype} can't be grown here"
            return
        self.node = "/dev/" + n
        if n.startswith("dm-"):
            self._probe_lvm(n)
        elif self._is_part(n):
            self.kind = "part"
            self.parts = [(self._disk_of(n), _int(f"{SYS}/class/block/{n}/partition"), n)]
        elif n.startswith(("md", "loop", "zd", "nbd")):
            self.why = "raid/loop/zfs volumes aren't handled"
        else:
            self.why = "whole-disk filesystem"   # nothing to grow into: the filesystem already spans the disk

    def _probe_lvm(self, n):
        uuid = _read(f"{SYS}/class/block/{n}/dm/uuid", "")
        name = _read(f"{SYS}/class/block/{n}/dm/name", "")
        m = re.fullmatch(r"((?:[^-]|--)+)-((?:[^-]|--)+)", name)
        if not uuid.startswith("LVM-") or len(uuid) != 4 + 64 or not m:
            self.why = "not a plain LVM volume (encrypted / thin / snapshot)"
            return
        vg, lv = (x.replace("--", "-") for x in m.groups())
        self.lv = f"/dev/{vg}/{lv}"
        # every LV of this VG: sizes (and refuse thin/snapshot/cache layouts, whose sizes don't add up)
        lvs = 0
        for d in sorted(os.listdir(f"{SYS}/class/block")):
            if not d.startswith("dm-"):
                continue
            u = _read(f"{SYS}/class/block/{d}/dm/uuid", "")
            nm = _read(f"{SYS}/class/block/{d}/dm/name", "")
            if not nm.startswith(m.group(1) + "-") or not u.startswith("LVM-"):
                continue
            if len(u) != 68:
                self.why = "LVM thin/snapshot/cache volumes aren't handled"
                return
            lvs += (_int(f"{SYS}/class/block/{d}/size") or 0) * 512
        self.lv_total = lvs
        pvs = set()
        for d in sorted(os.listdir(f"{SYS}/class/block")):
            if d.startswith("dm-") and (_read(f"{SYS}/class/block/{d}/dm/name", "") or "").startswith(m.group(1) + "-"):
                pvs.update(os.listdir(f"{SYS}/class/block/{d}/slaves"))
        if not pvs:
            self.why = "no physical volume found"
            return
        for pv in sorted(pvs):
            if pv.startswith("dm-") or pv.startswith("md"):
                self.why = "stacked LVM/raid/encryption isn't handled"
                return
            self.pv_total += (_int(f"{SYS}/class/block/{pv}/size") or 0) * 512 - MiB   # ~1 MiB of LVM metadata
            self.parts.append((self._disk_of(pv) if self._is_part(pv) else pv,
                               _int(f"{SYS}/class/block/{pv}/partition") if self._is_part(pv) else None, pv))
        self.kind = "lvm"

    # -- free space
    def gap(self, disk, num, node):
        """Bytes of empty disk right after this partition (0 for a whole-disk PV: it grows by itself)."""
        if num is None:
            return 0
        base = f"{SYS}/block/{disk}"
        size = (_int(f"{base}/size") or 0)
        mine = None
        others = []
        for d in os.listdir(base):
            if os.path.exists(f"{base}/{d}/partition"):
                s, z = _int(f"{base}/{d}/start"), _int(f"{base}/{d}/size")
                if s is None or z is None:
                    continue
                if z <= 2:
                    return 0        # msdos extended container / logical partitions: leave those alone
                if d == node:
                    mine = (s, z)
                else:
                    others.append(s)
        if not mine:
            return 0
        end = mine[0] + mine[1]
        nxt = min([s for s in others if s >= end] or [size])
        return max(0, nxt - end) * 512

    def last(self, disk, num, node):
        """True when no partition follows this one (so the disk can only ever grow into the space after it)."""
        if num is None:
            return False
        base = f"{SYS}/block/{disk}"
        mine = (_int(f"{base}/{node}/start") or 0) + (_int(f"{base}/{node}/size") or 0)
        return not any(d != node and os.path.exists(f"{base}/{d}/partition") and (_int(f"{base}/{d}/start") or 0) >= mine
                       for d in os.listdir(base))

    def free(self):
        """Bytes this filesystem could gain: empty disk after its partition(s), plus (LVM) unused extents."""
        if self.kind is None:
            return 0
        gaps = sum(self.gap(*p) for p in self.parts)
        if self.kind == "part":
            return gaps
        return gaps + max(0, self.pv_total - self.lv_total)

    def steps(self, assume_free=False):
        """Argument lists (absolute binaries) to run, in order. assume_free lists every possible step (for sudoers)."""
        if self.kind is None:
            return []
        out = []
        for disk, num, node in self.parts:
            if num is not None and ((self.last(disk, num, node) or self.gap(disk, num, node) >= min_bytes()) if assume_free else self.gap(disk, num, node) >= min_bytes()):
                out.append([exe("growpart"), "/dev/" + disk, str(num)])
        if self.kind == "part":
            if assume_free and not out:
                return []       # a partition with another one right behind it can never grow
            out.append([exe("xfs_growfs"), self.mount] if self.fstype == "xfs" else [exe("resize2fs"), self.node])
        else:
            for disk, num, node in self.parts:
                out.append([exe("pvresize"), "/dev/" + node])
            out.append([exe("lvextend"), "-l", "+100%FREE", "-r", self.lv])
        return out

    def disks(self):
        return sorted({d for d, _, _ in self.parts})


def inspect(dev, mount, fstype):
    """Summary for the drive tile: None when this filesystem can't be grown, else {"free": bytes}."""
    lay = Layout(dev, mount, fstype)
    if lay.kind is None:
        return None
    free = lay.free()
    return {"free": free if free >= min_bytes() else 0}


def layouts():
    out = []
    try:
        with open("/proc/self/mounts") as f:
            rows = [l.split()[:3] for l in f if l.strip()]
    except OSError:
        return out
    seen = set()
    for dev, mp, fs in rows:
        if not dev.startswith("/dev/") or dev in seen:
            continue
        seen.add(dev)
        mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), mp)
        lay = Layout(dev, mp, fs)
        if lay.kind:
            out.append(lay)
    return out


def rescan_path(disk):
    p = f"{SYS}/block/{disk}/device/rescan"
    return p if os.path.exists(p) else None


def sudoers_commands():
    """Every command line the grow/check buttons may need, for all growable mounts (whether or not they have free
    space now - it can appear later). Each is already sudoers-escaped."""
    cmds, seen = [], set()
    for lay in layouts():
        for argv in lay.steps(assume_free=True):
            line = " ".join(esc(a) for a in argv)
            if line not in seen:
                seen.add(line); cmds.append(line)
        for disk in (lay.disks() if lay.steps(assume_free=True) else []):
            if rescan_path(disk):
                line = f"{exe('tee')} {esc(rescan_path(disk))}"
                if line not in seen:
                    seen.add(line); cmds.append(line)
    return cmds


if __name__ == "__main__":
    if "--sudoers" in sys.argv:
        print("\n".join(sudoers_commands()))
    else:
        for lay in layouts():
            f = lay.free()
            print(f"{lay.mount:<14} {lay.fstype:<5} {lay.kind:<5} free {f / MiB:>9.0f} MiB  "
                  f"{'-> ' + ' ; '.join(' '.join(s) for s in lay.steps()) if f >= min_bytes() else ''}")
