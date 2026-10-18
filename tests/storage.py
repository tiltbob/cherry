#!/usr/bin/env python3
"""package/cherry/cherry-storage claims only the disks it should, drives LVM as the README says, and gives /var the
pool cherry: in RAM at boot, on the pool's disks once extend moved it there.

The helper runs against a stand-in sysfs (with a zram-control that hands out zram1), udev database, mount table and
swap list in a temporary directory, with stand-ins on PATH for lvm, wipefs, systemd-repart, systemd-cryptsetup,
udevadm, mkfs.btrfs, btrfs, mount, umount and df. They are this file, under those names:
they keep the machine's state (signatures, which partitions the TPM unlocks, LVM's physical volumes, volume groups and
logical volumes, with the extents each takes on each physical volume) in a JSON file, change the stand-in sysfs as the
real commands would (systemd-repart adds the cherry-pv partition, systemd-cryptsetup the device-mapper device that
holds it), and record the calls that change something. The stand-in LVM refuses what LVM refuses: allocating more than
there is, reducing a physical volume still in use, moving or growing a cached volume, activating a volume with extents
on a missing physical volume (one whose device is not in the sysfs), removing a missing one still in use. Needs python3,
sh, coreutils, awk, sed and util-linux's flock.
"""

import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "package", "cherry", "cherry-storage")
STANDINS = ["lvm", "wipefs", "systemd-repart", "cryptsetup", "systemd-cryptenroll", "systemd-cryptsetup", "mkfs.btrfs",
            "btrfs", "mount", "umount"]
# Those that only answer, in sh.
SH_STANDINS = {
    "udevadm": "",
    "partprobe": "",
    "df": "printf '     1B-blocks       Used      Avail\\n  10737418240  1073741824  9663676416\\n'",
}
EXTENT = 4 << 20
MIB = 1 << 20
GIB = 1 << 30
# The zram device the stand-in zram-control hands out, for the pool cherry in RAM.
ZRAM = "/dev/zram1"


# --- The stand-ins -------------------------------------------------------------------------------------------------

class Exit(Exception):
    def __init__(self, code, message=""):
        self.code, self.message = code, message


def load():
    with open(os.environ["STORAGE_STATE"]) as f:
        return json.load(f)


def save(state):
    with open(os.environ["STORAGE_STATE"], "w") as f:
        json.dump(state, f)


def block(*parts):
    return os.path.join(os.environ["CHERRY_SYSFS"], "class", "block", *parts)


def read(*parts):
    with open(block(*parts)) as f:
        return f.read().strip()


def to_bytes(size):
    return int(size[:-1]) << {"M": 20, "G": 30, "T": 40}[size[-1].upper()]


def holders(dev):
    return os.listdir(block(dev, "holders"))


def lv_alloc(state, pv):
    return sum(lv["alloc"].get(pv, 0) for lv in state["lvs"].values())


def present(pv):
    """Whether the physical volume's device is there: the device-mapper device of that name, or the zram device."""
    if pv.startswith("/dev/mapper/"):
        return any(os.path.exists(block(dm, "dm", "name")) and read(dm, "dm", "name") == pv[len("/dev/mapper/"):]
                   for dm in os.listdir(block()))
    return os.path.isdir(block(pv.removeprefix("/dev/")))


def missing_pvs(state, vg):
    return [p for p, v in state["pvs"].items() if v["vg"] == vg and not present(p)]


def partial(state, lv):
    return any(not present(p) for p in lv["alloc"])


def allocate(state, vg, lv, extents, pvs):
    """extents more for lv, first fit on pvs (or all of the vg's)."""
    pvs = pvs or [p for p, v in state["pvs"].items() if v["vg"] == vg]
    free = {p: state["pvs"][p]["extents"] - lv_alloc(state, p) for p in pvs}
    if extents <= 0 or sum(free.values()) < extents:
        raise Exit(5, f"Insufficient free space: {extents} extents needed, but only {sum(free.values())} available")
    for p in pvs:
        take = min(free[p], extents)
        if take:
            lv["alloc"][p] = lv["alloc"].get(p, 0) + take
            extents -= take


def extents_of(args, state, vg, pvs, current=0):
    if "-L" in args:
        return -(-to_bytes(args[args.index("-L") + 1]) // EXTENT) - current
    value = args[args.index("-l") + 1]
    if value.endswith("%PVS"):
        pvs = pvs or [p for p, v in state["pvs"].items() if v["vg"] == vg]
        return sum(state["pvs"][p]["extents"] - lv_alloc(state, p) for p in pvs)
    return int(value)


def lvm(state, args):
    cmd, args = args[0], args[1:]
    pvs, vgs, lvs = state["pvs"], state["vgs"], state["lvs"]
    if cmd in ("pvs", "vgs", "lvs"):
        return report(state, cmd, args)
    state["calls"].append(" ".join(["lvm", cmd, *args]))
    if cmd in state["fail"]:
        raise Exit(5, f"{cmd} failing as told")
    words = [a for a in args if not a.startswith("-")]
    if cmd == "pvcreate":
        if words[0].startswith("/dev/zram"):
            # LVM's metadata takes the first MiB; the extents follow.
            extents = (int(read(words[0].removeprefix("/dev/"), "disksize")) - MIB) // EXTENT
        else:
            extents = int(read(words[0].removeprefix("/dev/mapper/cherry-pv-"), "size")) * 512 // EXTENT
        pvs[words[0]] = {"vg": "", "tags": [], "extents": extents}
    elif cmd == "pvremove":
        if pvs[words[0]]["vg"]:
            raise Exit(5, f"PV {words[0]} belongs to Volume Group {pvs[words[0]]['vg']}")
        del pvs[words[0]]
    elif cmd in ("vgcreate", "vgextend"):
        vg, new = (words[1], words[2:]) if cmd == "vgcreate" else (words[0], words[1:])
        if cmd == "vgcreate":
            assert args[:2] == ["--setautoactivation", "n"], args
            vgs[vg] = {"tags": [], "active": True}
        for pv in new:
            if pvs[pv]["vg"]:
                raise Exit(5, f"Physical volume {pv} is already in volume group {pvs[pv]['vg']}")
            pvs[pv]["vg"] = vg
    elif cmd == "vgreduce":
        if "--removemissing" in args:
            for pv in missing_pvs(state, words[0]):
                if lv_alloc(state, pv):
                    raise Exit(5, f"WARNING: Partial LV needs to be repaired or removed. There are still partial LVs "
                                  f"in VG {words[0]}. To remove them unconditionally use: vgreduce --removemissing "
                                  "--force.")
                del pvs[pv]
            return
        if lv_alloc(state, words[1]):
            raise Exit(5, f"Physical volume {words[1]} still in use")
        pvs[words[1]].update(vg="", tags=[])
    elif cmd == "vgremove":
        for key in [k for k in lvs if k.startswith(words[0] + "/")]:
            del lvs[key]
        for pv in pvs.values():
            if pv["vg"] == words[0]:
                pv.update(vg="", tags=[])
        del vgs[words[0]]
    elif cmd == "pvchange":
        tag = args[args.index("--addtag") + 1] if "--addtag" in args else None
        for pv in words[1:]:
            if not pvs[pv]["vg"]:
                raise Exit(5, f"Unable to add tag to orphan PV {pv}")
            pvs[pv]["tags"].append(tag)
    elif cmd == "vgchange":
        if "--addtag" in args:
            vgs[words[1]]["tags"].append(words[0])
        elif "--deltag" in args:
            vgs[words[1]]["tags"].remove(words[0])
        else:
            vgs[words[0]]["active"] = "-ay" in args
            # What is whole activates; a volume with extents on a missing PV does not (activation_mode degraded).
            if "-ay" in args and any(partial(state, lv) for k, lv in lvs.items() if k.startswith(words[0] + "/")):
                raise Exit(5, f"Refusing activation of partial LV in VG {words[0]}. Use '--activationmode partial' "
                              "to override.")
    elif cmd == "lvcreate":
        words = [w for w in words if w not in ("cache-pool", "n")]
        name, vg = args[args.index("-n") + 1], words[2] if "-L" in args or "-l" in args else words[1]
        targets = words[words.index(vg) + 1:]
        pool = "--type" in args
        if pool:
            assert args[args.index("--type") + 1] == "cache-pool" and "--poolmetadataspare" in args, args
        lv = {"segtype": "cache-pool" if pool else "linear", "alloc": {}, "hidden": False, "cvol": None, "mode": ""}
        # A cache pool's metadata: 2 extents more.
        allocate(state, vg, lv, extents_of(args, state, vg, targets) + (2 if pool else 0), targets)
        lvs[f"{vg}/{name}"] = lv
    elif cmd == "lvextend":
        key = next(w for w in words if "/" in w and not w.startswith("/dev/"))
        lv, targets = lvs[key], [w for w in words if w.startswith("/dev/")]
        if lv["segtype"] == "cache":
            raise Exit(5, "Unable to resize logical volumes of cache type")
        more = extents_of(args, state, key.split("/")[0], targets, sum(lv["alloc"].values()))
        if "-L" in args and more <= 0:
            raise Exit(5, "New size given is not larger than existing size")
        allocate(state, key.split("/")[0], lv, more, targets)
    elif cmd == "lvconvert":
        key = words[-1]
        vg, lv = key.split("/")[0], lvs[key]
        if "--uncache" in args:
            del lvs[f"{vg}/{lv['cvol']}"]
            lv.update(segtype="linear", cvol=None, mode="")
        else:
            cpool = args[args.index("--cachepool") + 1]
            cvol = lvs.pop(cpool)
            assert cvol["segtype"] == "cache-pool", cvol
            cvol["hidden"] = True
            name = f"[{cpool.split('/')[1]}_cpool]"
            lvs[f"{vg}/{name}"] = cvol
            lv.update(segtype="cache", cvol=name, mode=args[args.index("--cachemode") + 1])
    elif cmd == "lvremove":
        lv = lvs.pop(words[0])
        if lv["cvol"]:
            del lvs[f"{words[0].split('/')[0]}/{lv['cvol']}"]
    elif cmd == "pvmove":
        if "--abort" in args:
            return
        source, targets = words[0], words[1:]
        for key, lv in list(lvs.items()):
            if lv["alloc"].get(source):
                if lv["segtype"] == "cache" or lv["hidden"]:
                    raise Exit(5, f"Unable to pvmove when cache LVs are present ({key})")
                allocate(state, pvs[source]["vg"], lv, lv["alloc"].pop(source), targets)
    elif cmd not in ("vgchange",):
        raise Exit(3, f"lvm stand-in: no {cmd}")


def report(state, cmd, args):
    """pvs, vgs and lvs: what cherry-storage asks them, as --noheadings with --separator '|', or as JSON."""
    pvs, vgs, lvs = state["pvs"], state["vgs"], state["lvs"]
    fields = args[args.index("-o") + 1].split(",")
    values = {i + 1 for i, a in enumerate(args) if a in ("-o", "--separator", "--units", "--reportformat")}
    only = [a for i, a in enumerate(args) if not a.startswith("-") and i not in values]
    if cmd == "pvs":
        rows = [{"pv_name": p, "vg_name": v["vg"], "pv_tags": ",".join(v["tags"]),
                 "pv_pe_count": v["extents"] if v["vg"] else 0, "pv_pe_alloc_count": lv_alloc(state, p),
                 "pv_size": v["extents"] * EXTENT, "pv_free": (v["extents"] - lv_alloc(state, p)) * EXTENT}
                for p, v in sorted(pvs.items())]
    elif cmd == "vgs":
        # A volume group none of whose devices is there is not seen at all.
        rows = []
        for name, vg in sorted(vgs.items()):
            mine = [p for p, v in pvs.items() if v["vg"] == name]
            gone = missing_pvs(state, name)
            if mine and len(gone) == len(mine):
                continue
            size = sum(pvs[p]["extents"] for p in mine) * EXTENT
            rows.append({"vg_name": name, "vg_tags": ",".join(vg["tags"]), "vg_extent_size": EXTENT,
                         "vg_size": size, "vg_free": size - sum(lv_alloc(state, p) for p in mine) * EXTENT,
                         "pv_count": len(mine), "lv_count": sum(k.startswith(name + "/") for k in lvs),
                         "vg_missing_pv_count": len(gone)})
    else:
        rows = [{"vg_name": k.split("/")[0], "lv_name": k.split("/")[1], "lv_size": sum(v["alloc"].values()) * EXTENT,
                 "segtype": v["segtype"], "cache_mode": v["mode"], "pool_lv": v["cvol"] or "", "lv_attr": "-wi-a-----",
                 "lv_active": "active" if vgs[k.split("/")[0]]["active"] and not partial(state, v) else "",
                 "devices": ""}
                for k, v in sorted(lvs.items()) if "-a" in args or not v["hidden"]]
    rows = [r for r in rows if not only or r["vg_name"] in only]
    if "--reportformat" in args:
        print(json.dumps({"report": [{cmd[:2]: [{f: str(r[f]) for f in fields} for r in rows]}]}))
    elif "--separator" in args:
        for r in rows:
            print("  " + "|".join(str(r[f]) for f in fields))
    else:
        # Aligned, as LVM prints without a separator: each column padded to its widest value.
        cells = [[str(r[f]) for f in fields] for r in rows]
        widths = [max((len(c[i]) for c in cells), default=0) for i in range(len(fields))]
        for c in cells:
            print("  " + " ".join(v.ljust(w) for v, w in zip(c, widths)))


def wipefs(state, args):
    dev = args[-1].removeprefix("/dev/")
    found = state["signatures"].get(dev, [])
    if "--no-act" in args:
        if dev in state["fail"]:
            raise Exit(1, f"wipefs: {dev}: failing as told")
        output = args[[a.startswith("--output=") for a in args].index(True)].split("=")[1].split(",")
        for sig in found:
            print(" ".join(sig[f] for f in output).rstrip())
        return
    state["calls"].append(" ".join(["wipefs", *args]))
    if os.path.exists(block(dev, "device")) and dev not in state["stale"]:
        for part in os.listdir(block(dev)):
            if os.path.exists(block(dev, part, "partition")):
                if holders(part):
                    raise Exit(1, f"wipefs: error: /dev/{dev}: probing initialization failed: Device or resource busy")
                shutil.rmtree(block(dev, part))
                shutil.rmtree(block(part))
    state["signatures"][dev] = []


def repart(state, args):
    state["calls"].append(" ".join(["systemd-repart", *args]))
    disk = args[-1].removeprefix("/dev/")
    if "systemd-repart" in state["fail"]:
        raise Exit(1, "systemd-repart failing as told")
    if "--empty=require" in args and state["signatures"].get(disk):
        raise Exit(1, f"Disk {args[-1]} has a partition table already")
    part = disk + ("p1" if disk[-1].isdigit() else "1")
    os.makedirs(block(disk, part))
    open(block(disk, part, "partition"), "w").write("1\n")
    make_device(state, part, int(read(disk, "size")), "DEVTYPE=partition\nPARTNAME=cherry-pv\n")
    state["signatures"][disk] = [{"UUID": f"new-gpt-{disk}", "TYPE": "gpt", "OFFSET": "0x200"},
                                 {"UUID": "", "TYPE": "PMBR", "OFFSET": "0x1fe"}]


def luks(state, args, name):
    """cryptsetup luksFormat with a key file, then systemd-cryptenroll with it, which seals a new key slot's key in
    the TPM and wipes the key file's."""
    state["calls"].append(" ".join([name, *args]))
    part = args[-1].removeprefix("/dev/")
    if name in state["fail"]:
        raise Exit(1, f"{name} failing as told")
    key = args[args.index("--key-file") + 1] if name == "cryptsetup" else args[0].split("=", 1)[1]
    with open(key, "rb") as f:
        check(len(f.read()) == 64, f"{name}: the key in {key} is not 64 bytes")
    if name == "cryptsetup":
        state["signatures"][part] = [{"UUID": f"luks-{part}", "TYPE": "crypto_LUKS", "OFFSET": "0x0"}]


def make_device(state, name, sectors, uevent, partition=True):
    os.makedirs(block(name, "holders"), exist_ok=True)
    state["minor"] += 1
    for f, value in (("size", sectors), ("dev", f"8:{state['minor']}"), ("uevent", uevent)):
        with open(block(name, f), "w") as out:
            out.write(f"{value}\n" if f != "uevent" else value)
    if partition:
        open(block(name, "partition"), "w").write("1\n")


def cryptsetup(state, args):
    state["calls"].append(" ".join(["systemd-cryptsetup", *args]))
    if args[0] == "attach":
        part = args[2].removeprefix("/dev/")
        if part in state["locked"]:
            raise Exit(1, f"Failed to unlock {args[2]} with the TPM")
        dm = f"dm-{state['minor']}"
        make_device(state, dm, read(part, "size"), "DEVTYPE=disk\n", partition=False)
        os.makedirs(block(dm, "dm"))
        open(block(dm, "dm", "name"), "w").write(args[1] + "\n")
        open(block(part, "holders", dm), "w").close()
    else:
        for dm in os.listdir(block()):
            if os.path.exists(block(dm, "dm", "name")) and read(dm, "dm", "name") == args[1]:
                for part in os.listdir(block()):
                    if os.path.exists(block(part, "holders", dm)):
                        os.remove(block(part, "holders", dm))
                shutil.rmtree(block(dm))


def mounts(state, args, name):
    path = os.environ["CHERRY_MOUNTINFO"]
    with open(path) as f:
        lines = f.read().splitlines()
    state["calls"].append(" ".join([name, *args]))
    if name in state["fail"]:
        raise Exit(32, f"{name}: failing as told")
    if name == "mount":
        dev, mp = args[-2], args[-1]
        if mp in state["nomount"]:
            raise Exit(32, f"mount: {mp}: wrong fs type, bad option, bad superblock on {dev}")
        fstype = args[args.index("-t") + 1]
        lines.append(f"{100 + len(lines)} 1 253:{len(lines)} / {mp} rw,nodev,nosuid - {fstype} {dev} rw")
    else:
        if args[0] in state["busy"]:
            raise Exit(32, f"umount: {args[0]}: target is busy.")
        lines = [line for line in lines if line.split()[4] != args[0]]
    with open(path, "w") as f:
        f.write("".join(line + "\n" for line in lines))


def standin(name, args):
    state = load()
    try:
        if name == "lvm":
            lvm(state, args)
        elif name == "wipefs":
            wipefs(state, args)
        elif name == "systemd-repart":
            repart(state, args)
        elif name == "systemd-cryptsetup":
            cryptsetup(state, args)
        elif name in ("cryptsetup", "systemd-cryptenroll"):
            luks(state, args, name)
        elif name in ("mount", "umount"):
            mounts(state, args, name)
        elif name in ("mkfs.btrfs", "btrfs"):
            state["calls"].append(" ".join([name, *args]))
        save(state)
    except Exit as e:
        save(state)
        if e.message:
            print(e.message, file=sys.stderr)
        sys.exit(e.code)


# --- The test --------------------------------------------------------------------------------------------------------

class Machine:
    """The stand-in machine: a sysfs, udev database, mount table, swaps, TPM identity and state, in tmp; fresh, as
    cherry-storage.service leaves it at boot without disks, with the pool cherry in RAM and /var mounted from it."""

    def __init__(self, tmp):
        self.tmp = tmp
        user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        self.env = dict(os.environ, CHERRY_SYSFS=f"{tmp}/sys", CHERRY_UDEV=f"{tmp}/udev",
                        CHERRY_MOUNTINFO=f"{tmp}/mountinfo", CHERRY_SWAPS=f"{tmp}/swaps",
                        CHERRY_IDENTITY=f"{tmp}/identity", CHERRY_VOLUMES=f"{tmp}/home/volumes",
                        CHERRY_STORAGE_USER=user,
                        CHERRY_STORAGE_LOCK=f"{tmp}/lock", LOGS_DIRECTORY=f"{tmp}/logs", STORAGE_STATE=f"{tmp}/state",
                        PATH=f"{tmp}/bin:{os.environ['PATH']}")
        os.makedirs(f"{tmp}/bin")
        for name in STANDINS:
            with open(f"{tmp}/bin/{name}", "w") as f:
                f.write(f'#!/bin/sh\nexec python3 -S {os.path.abspath(__file__)} {name} "$@"\n')
        for name, body in SH_STANDINS.items():
            with open(f"{tmp}/bin/{name}", "w") as f:
                f.write(f"#!/bin/sh\n{body}\n")
        for name in [*STANDINS, *SH_STANDINS]:
            os.chmod(f"{tmp}/bin/{name}", 0o755)
        os.environ.update({k: v for k, v in self.env.items() if k.startswith(("CHERRY_", "STORAGE_"))})
        self.fresh()

    def fresh(self, ram_var=True):
        for d in ("sys", "udev", "identity", "home", "logs"):
            shutil.rmtree(f"{self.tmp}/{d}", ignore_errors=True)
        os.makedirs(block())
        for d in ("udev", "identity", "logs", "home"):
            os.makedirs(f"{self.tmp}/{d}")
        open(f"{self.tmp}/identity/ek-hash", "w").write("0" * 64 + "\n")
        open(f"{self.tmp}/mountinfo", "w").write("")
        open(f"{self.tmp}/swaps", "w").write("Filename\tType\tSize\tUsed\tPriority\n/dev/zram0\tpartition\t1\t0\t100\n")
        # zram0 is the swap; zram-control hands out zram1, an empty device until it is given a size.
        os.makedirs(f"{self.tmp}/sys/class/zram-control")
        open(f"{self.tmp}/sys/class/zram-control/hot_add", "w").write("1\n")
        os.makedirs(block("zram1", "holders"))
        save({"calls": [], "pvs": {}, "vgs": {}, "lvs": {}, "signatures": {}, "locked": [], "fail": [], "busy": [],
              "nomount": [], "stale": [], "minor": 0})
        if ram_var:
            rc, out = self.run("boot", "start")
            check(rc == 0, f"the pool cherry in RAM: {out}")
            self.change(calls=[])

    def zram_control(self, name):
        with open(f"{self.tmp}/sys/class/zram-control/{name}") as f:
            return f.read().strip()

    def unmount_var(self):
        """As systemd does at shutdown before `boot stop`: /var unmounted (the volumes under it are left to the stop,
        which unmounts what is still mounted)."""
        with open(f"{self.tmp}/mountinfo") as f:
            lines = [line for line in f.read().splitlines() if line.split()[4] != "/var"]
        with open(f"{self.tmp}/mountinfo", "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def reboot(self):
        """After `boot stop`: RAM is gone, and with it the zram device and whatever pool lived on it alone."""
        state = load()
        ram = [vg for vg in state["vgs"] if all(p.startswith("/dev/zram") for p, v in state["pvs"].items()
                                                if v["vg"] == vg)]
        for vg in ram:
            del state["vgs"][vg]
            for key in [k for k in state["lvs"] if k.startswith(vg + "/")]:
                del state["lvs"][key]
        for pv in [p for p, v in state["pvs"].items() if v["vg"] in ram]:
            del state["pvs"][pv]
        save(state)
        shutil.rmtree(block("zram1"), ignore_errors=True)
        os.makedirs(block("zram1", "holders"))

    def shutdown(self):
        self.unmount_var()
        self.run("boot", "stop")
        self.reboot()

    def state(self):
        return load()

    def change(self, **values):
        state = load()
        state.update(values)
        save(state)

    def disk(self, name, gib=100, ssd=False, bus="pci0000:00/virtio0", serial=None, removable=0, ro=0):
        """A whole disk on a device, writable, not removable, blank."""
        device = os.path.join(self.env["CHERRY_SYSFS"], "devices", bus, name)
        os.makedirs(device)
        state = load()
        make_device(state, name, gib * GIB // 512, "DEVTYPE=disk\n", partition=False)
        save(state)
        os.symlink(device, block(name, "device"))
        os.makedirs(block(name, "queue"))
        for f, value in (("removable", removable), ("ro", ro), ("queue/rotational", 0 if ssd else 1)):
            open(block(name, f), "w").write(f"{value}\n")
        with open(f"{self.tmp}/udev/b{read(name, 'dev')}", "w") as f:
            f.write(f"E:ID_MODEL=Model_{name}\nE:ID_SERIAL_SHORT={serial or 'S' + name}\n"
                    f"E:ID_SERIAL=Model_{name}_{serial or 'S' + name}\nE:ID_BUS=ata\n")

    def part(self, disk, number, label="", fstype=""):
        name = disk + (f"p{number}" if disk[-1].isdigit() else str(number))
        os.makedirs(block(disk, name))
        open(block(disk, name, "partition"), "w").write(f"{number}\n")
        state = load()
        make_device(state, name, 2 * GIB // 512, f"DEVTYPE=partition\n{'PARTNAME=' + label if label else ''}\n")
        sigs = state["signatures"].setdefault(disk, [])
        if not sigs:
            sigs.append({"UUID": f"gpt-{disk}", "TYPE": "gpt", "OFFSET": "0x200"})
        if fstype:
            state["signatures"][name] = [{"UUID": f"fs-{name}", "TYPE": fstype, "OFFSET": "0x438"}]
            with open(f"{self.tmp}/udev/b{read(name, 'dev')}", "w") as f:
                f.write(f"E:ID_FS_TYPE={fstype}\n")
        save(state)
        return name

    def run(self, *args):
        """cherry-storage with these arguments: (exit status, its output and log)."""
        state = load()
        state["calls"] = []
        save(state)
        p = subprocess.run(["sh", SCRIPT, *args], env=self.env, capture_output=True, text=True)
        log = ""
        if len(args) == 1:
            try:
                with open(f"{self.tmp}/logs/{args[0]}.log") as f:
                    log = f.read()
            except FileNotFoundError:
                pass
        return p.returncode, p.stdout + p.stderr + log

    def list(self):
        rc, out = self.run("list")
        check(rc == 0, f"list failed: {out}")
        with open(f"{self.tmp}/logs/list.json") as f:
            return json.load(f)

    def fingerprint(self, disk):
        return next(d["fingerprint"] for d in self.list()["disks"] if d["name"] == disk)

    def calls(self):
        return load()["calls"]


failures = []
cases = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def case(name):
    def wrap(fn):
        global cases
        try:
            fn()
            cases += 1
        except AssertionError as e:
            failures.append(f"{name}: {e}")
        return fn
    return wrap


def expect(name, result, rc, line, calls=None):
    code, out = result
    check(code == rc, f"{name}: exit status {code}, not {rc}:\n{out}")
    check(line in out, f"{name}: no {line!r} in:\n{out}")
    if calls is not None:
        got = m.calls()
        check(got == calls, f"{name}: called\n  " + "\n  ".join(got) + "\nnot\n  " + "\n  ".join(calls))


POOL = "--type cache-pool --poolmetadataspare n"
REPART = ("systemd-repart --dry-run=no --empty=require --definitions=/usr/lib/cherry/storage/repart.d "
          "--tpm2-device=auto --tpm2-pcrs= --tpm2-public-key-pcrs=")


def luks_calls(part):
    """What makes the partition a LUKS2 volume that only the TPM unlocks, with a key file in some temporary
    directory."""
    calls = [c for c in m.calls() if c.startswith(("cryptsetup ", "systemd-cryptenroll "))]
    check(len(calls) == 2, f"no cryptsetup and systemd-cryptenroll calls: {m.calls()}")
    key = calls[0].split("--key-file ")[1].split()[0]
    check(not os.path.exists(key), f"the key file {key} is left")
    return [f"cryptsetup luksFormat --batch-mode --type luks2 --pbkdf pbkdf2 --pbkdf-force-iterations 1000 "
            f"--key-file {key} /dev/{part}",
            f"systemd-cryptenroll --unlock-key-file={key} --tpm2-device=auto --tpm2-pcrs= --tpm2-public-key-pcrs= "
            f"--wipe-slot=0 /dev/{part}"]


def claim(*disks):
    """Each disk claimed, with the fingerprint it is listed with: the paths of their physical volumes."""
    fingerprints = {d["name"]: d["fingerprint"] for d in m.list()["disks"]}
    pvs = []
    for disk in disks:
        rc, out = m.run(f"claim:{disk}:{fingerprints[disk]}")
        check(rc == 0, f"claim {disk}: {out}")
        pvs.append(f"/dev/mapper/cherry-pv-{disk}{'p1' if disk[-1].isdigit() else '1'}")
    return pvs


def mib(extents):
    return extents * EXTENT >> 20


def volume(name):
    return m.state()["lvs"][name]


def main():
    # Each stand-in call starts this file anew: what only the test needs is imported here, not for them.
    global m, fcntl, subprocess
    import fcntl
    import subprocess
    import tempfile
    tmp = tempfile.mkdtemp(prefix="cherry-storage-")
    try:
        m = Machine(tmp)
        tests()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print("\n".join(f"storage: {f}" for f in failures), file=sys.stderr)
        print(f"storage: {len(failures)} of {len(failures) + cases} cases failed", file=sys.stderr)
        sys.exit(1)
    print(f"storage: {cases} cases passed")


def tests():
    @case("list: every disk, in its state")
    def _():
        m.fresh()
        m.disk("sda")
        m.disk("sdb", ssd=True)
        m.part("sdb", 1, fstype="vfat")
        # A disk an older image made its /var: nothing special now.
        m.disk("sdc")
        m.part("sdc", 1, label="cherry-var")
        m.disk("sdd")
        part = m.part("sdd", 1, fstype="xfs")
        open(f"{tmp()}/mountinfo", "a").write(f"40 25 {read(part, 'dev')} / /mnt rw - xfs /dev/{part} rw\n")
        m.disk("sde")
        m.part("sde", 1, fstype="swap")
        open(f"{tmp()}/swaps", "a").write("/dev/sde1\tpartition\t1\t0\t-2\n")
        m.disk("sdf")
        open(block("sdf", "holders", "md127"), "w").close()
        m.disk("sdg", removable=1)
        m.disk("sdh", ro=1)
        m.disk("sdi", bus="pci0000:00/usb1/1-1")
        m.disk("sdj", gib=0)
        m.disk("nvme0n1", ssd=True)
        m.part("nvme0n1", 1, label="cherry-pv")
        # Not disks: no device behind them.
        os.makedirs(block("loop0", "holders"))
        facts = m.list()
        states = {d["name"]: (d["state"], d["reason"]) for d in facts["disks"]}
        check(states == {
            "nvme0n1": ("locked", "its cherry-pv volume does not unlock with this machine's TPM: another machine's "
                                  "disk, or this one's before its TPM was cleared"),
            "sda": ("free", ""), "sdb": ("free", ""), "sdc": ("free", ""),
            "sdd": ("in-use", "/dev/sdd1 is mounted"), "sde": ("in-use", "/dev/sde1 is swap"),
            "sdf": ("in-use", "/dev/sdf is held by md127"), "sdg": ("unsuitable", "removable"),
            "sdh": ("unsuitable", "read-only"), "sdi": ("unsuitable", "on USB"),
            "sdj": ("unsuitable", "smaller than 1 GiB")}, f"states: {json.dumps(states, indent=1)}")
        sdb = next(d for d in facts["disks"] if d["name"] == "sdb")
        check(sdb["rotational"] is False and sdb["model"] == "Model sdb" and sdb["serial"] == "Ssdb"
              and sdb["size"] == 100 * GIB and sdb["signatures"] == ["gpt"]
              and sdb["partitions"] == [{"name": "sdb1", "size": 2 * GIB, "label": "", "fstype": "vfat"}], sdb)
        check(all(len(d["fingerprint"]) == 12 for d in facts["disks"]), "fingerprints")
        check(facts["var"] == {"fstype": "btrfs", "source": "/dev/mapper/cherry-var", "ram": True}
              and facts["tpm"] is True, facts)
        # The pool cherry in RAM: its one physical volume the zram device, tagged ram, all of it var's.
        check(facts["pvs"]["report"][0]["pv"] == [{"pv_name": ZRAM, "vg_name": "cherry", "pv_size": str(32 * EXTENT),
                                                   "pv_free": "0", "pv_tags": "ram"}], facts["pvs"])
        check([v["vg_name"] for v in facts["vgs"]["report"][0]["vg"]] == ["cherry"]
              and [(lv["lv_name"], lv["lv_size"]) for lv in facts["lvs"]["report"][0]["lv"]]
              == [("var", str(128 * MIB))], facts)
        check(facts["mounts"] == [{"path": "/var", "size": 10737418240, "used": 1073741824, "available": 9663676416}],
              facts["mounts"])
        check(m.calls() == [], f"list changed something: {m.calls()}")

    @case("claim refuses what it must, and changes nothing")
    def _():
        fps = {d["name"]: d["fingerprint"] for d in m.list()["disks"]}
        fp = fps["sda"]
        for instance, message in [
                ("claim:sda", "bad instance 'claim:sda'"),
                ("claim:sda:0123:x", "bad instance"),
                ("claim:sda/x:0123456789ab", "bad instance 'claim:sda/x:0123456789ab': letters, digits"),
                ("claim:sd.a:0123456789ab", "bad instance"),
                ("claim:sdA:0123456789ab", "bad disk name 'sdA'"),
                ("claim:sdz:0123456789ab", "no disk /dev/sdz"),
                ("claim:sdb1:0123456789ab", "no disk /dev/sdb1"),
                ("claim:sda:XYZ", "bad fingerprint 'XYZ'"),
                ("claim:sda:0123456789ab", "/dev/sda is not as it was listed (fingerprint 0123456789ab)"),
                ("claim:sdd:" + fps["sdd"], "/dev/sdd cannot be claimed: /dev/sdd1 is mounted"),
                ("claim:sdf:" + fps["sdf"], "/dev/sdf cannot be claimed: /dev/sdf is held by md127"),
                ("claim:sdi:" + fps["sdi"], "/dev/sdi cannot be claimed: on USB"),
                ("frobnicate:sda", "unknown verb 'frobnicate'"),
                ("boot", "bad instance 'boot'"),
                ("boot:start", "unknown verb 'boot'")]:
            expect(instance, m.run(instance), 1, message, [])
        os.remove(f"{tmp()}/identity/ek-hash")
        expect("no TPM", m.run(f"claim:sda:{fp}"), 1, "no usable TPM", [])
        open(f"{tmp()}/identity/ek-hash", "w").write("0" * 64 + "\n")

    @case("claim wipes the disk, partitions first, and makes it a physical volume")
    def _():
        expect("claim sdb", m.run(f"claim:sdb:{m.fingerprint('sdb')}"), 0,
               "claimed /dev/sdb: /dev/sdb1, unlocked with the TPM as /dev/mapper/cherry-pv-sdb1",
               ["wipefs --all /dev/sdb1", "wipefs --all /dev/sdb", f"{REPART} /dev/sdb", *luks_calls("sdb1"),
                "systemd-cryptsetup attach cherry-pv-sdb1 /dev/sdb1 - tpm2-device=auto,headless",
                "lvm pvcreate -y /dev/mapper/cherry-pv-sdb1"])
        sdb = next(d for d in m.list()["disks"] if d["name"] == "sdb")
        check(sdb["state"] == "claimed" and sdb["pv"] == "/dev/mapper/cherry-pv-sdb1", sdb)
        expect("again", m.run(f"claim:sdb:{sdb['fingerprint']}"), 1, "/dev/sdb cannot be claimed: claimed")
        # The locked disk, another machine's: claimed anew.
        expect("claim nvme0n1", m.run(f"claim:nvme0n1:{m.fingerprint('nvme0n1')}"), 0, "claimed /dev/nvme0n1")
        check(m.calls()[:2] == ["wipefs --all /dev/nvme0n1p1", "wipefs --all /dev/nvme0n1"], m.calls())

    @case("claim fails cleanly when the TPM cannot unlock the new volume, or systemd-repart fails")
    def _():
        for tool in ("cryptsetup", "systemd-cryptenroll"):
            m.change(fail=[tool])
            expect(tool, m.run(f"claim:sda:{m.fingerprint('sda')}"), 1,
                   "cannot make /dev/sda1 a LUKS2 volume that the TPM unlocks")
            check(not [c for c in m.calls() if c.startswith(("lvm", "systemd-cryptsetup"))], m.calls())
            # Left as a cherry-pv partition that no TPM unlocks: claimed again, it is wiped.
            check(next(d for d in m.list()["disks"] if d["name"] == "sda")["state"] == "locked", tool)
        m.change(fail=["systemd-repart"])
        expect("repart", m.run(f"claim:sda:{m.fingerprint('sda')}"), 1, "systemd-repart could not set up /dev/sda")
        check(not [c for c in m.calls() if c.startswith(("lvm", "systemd-cryptsetup"))], m.calls())
        m.change(fail=[], locked=["sda1"])
        expect("locked", m.run(f"claim:sda:{m.fingerprint('sda')}"), 1, "the TPM cannot unlock /dev/sda1")
        check(not [c for c in m.calls() if c.startswith("lvm")], m.calls())
        m.change(locked=[])
        # The kernel keeps sda's partition: wiped, but not set up.
        m.change(stale=["sda"])
        expect("stale", m.run(f"claim:sda:{m.fingerprint('sda')}"), 1,
               "/dev/sda is wiped, but the kernel still has its partitions: it is blank once the machine reboots",
               ["wipefs --all /dev/sda1", "wipefs --all /dev/sda"])
        m.change(stale=[])

    def setup():
        """A machine with three hard disks of 100 GiB and two SSDs of 10 GiB, all claimed: made once, then restored."""
        snapshot = f"{tmp()}/snapshot"
        if not os.path.exists(snapshot):
            m.fresh()
            for d in ("sda", "sdb", "sdc"):
                m.disk(d)
            for d in ("nvme0n1", "nvme1n1"):
                m.disk(d, gib=10, ssd=True)
            pvs = claim("sda", "sdb", "sdc", "nvme0n1", "nvme1n1")
            os.makedirs(snapshot)
            for f in MACHINE:
                copy(f"{tmp()}/{f}", f"{snapshot}/{f}")
            with open(f"{snapshot}/pvs", "w") as f:
                json.dump(pvs, f)
        for f in MACHINE:
            shutil.rmtree(f"{tmp()}/{f}", ignore_errors=True) if os.path.isdir(f"{tmp()}/{f}") else None
            copy(f"{snapshot}/{f}", f"{tmp()}/{f}")
        with open(f"{snapshot}/pvs") as f:
            return json.load(f)

    @case("create, extend: claimed disks only, tagged data")
    def _():
        sda, sdb, sdc, _, _ = setup()
        for instance, message in [
                ("create:my_pool:sda", "bad instance 'create:my_pool:sda'"),
                ("create:-tank:sda", "bad pool name '-tank'"),
                ("create:cherry:sda", "the pool cherry is the machine's own, there from boot: extend it"),
                ("create:" + "p" * 33 + ":sda", "longer than 32 characters"),
                ("create:tank", "bad instance"),
                ("create:tank:sdq", "no disk /dev/sdq")]:
            expect(instance, m.run(instance), 1, message, [])
        m.disk("sdd")
        expect("unclaimed", m.run("create:tank:sda:sdd"), 1, "/dev/sdd is not claimed (free)", [])
        expect("create", m.run("create:tank:sda:sdb"), 0, "created the pool tank",
               [f"lvm vgcreate --setautoactivation n tank {sda} {sdb}", f"lvm pvchange --addtag data {sda} {sdb}"])
        expect("exists", m.run("create:tank:sdc"), 1, "the pool tank exists already", [])
        expect("in a pool", m.run("create:other:sda"), 1, "/dev/sda is in the pool tank", [])
        expect("extend", m.run("extend:tank:sdc"), 0, "extended the pool tank",
               [f"lvm vgextend tank {sdc}", f"lvm pvchange --addtag data {sdc}"])
        expect("no pool", m.run("extend:nope:sdc"), 1, "no pool nope", [])

    @case("volumes: made on the data disks, owned by the cherry user, mounted")
    def _():
        sda, sdb, _, nvme0, _ = setup()
        m.run("create:tank:sda:sdb")
        for instance, message in [
                ("volume-create:tank:my_vol:10G", "bad instance"),
                ("volume-create:tank:-vol:10G", "bad volume name '-vol'"),
                ("volume-create:tank:vol:10", "bad size '10'"),
                ("volume-create:tank:vol:1.5T", "bad instance"),
                ("volume-create:tank:vol:0G", "bad size '0G'"),
                ("volume-create:nope:vol:10G", "no pool nope")]:
            expect(instance, m.run(instance), 1, message, [])
        shutil.rmtree(f"{tmp()}/home")
        mp = f"{tmp()}/home/volumes/tank/web"
        expect("create", m.run("volume-create:tank:web:150G"), 0, f"created the volume tank/web, mounted on {mp}",
               [f"lvm lvcreate -y -n web -L 150G tank {sda} {sdb}", "mkfs.btrfs -q /dev/tank/web",
                f"mount -t btrfs -o nodev,nosuid /dev/tank/web {mp}"])
        check(os.path.isdir(mp) and volume("tank/web")["alloc"] == {sda: 25600, sdb: 12800}, volume("tank/web"))
        # Its root the cherry user's, once mounted.
        check(os.stat(mp).st_uid == os.getuid(), f"{mp} is not the cherry user's")
        expect("exists", m.run("volume-create:tank:web:1G"), 1, "the pool tank has a volume web already")
        # LVM's own failure and exit status.
        expect("too big", m.run("volume-create:tank:big:100G"), 5, "Insufficient free space")
        check("tank/big" not in m.state()["lvs"], "a failed volume was left")
        m.change(fail=["mount"])
        expect("mount fails", m.run("volume-create:tank:third:1G"), 32, "mount: failing as told")
        m.change(fail=[])
        # all: the rest of the data disks, not the cache disks.
        m.run("cache:tank:writethrough:nvme0n1")
        expect("all", m.run("volume-create:tank:rest:all"), 0, "created the volume tank/rest")
        check(m.calls()[0] == f"lvm lvcreate -y -n rest -l 100%PVS tank {sda} {sdb}", m.calls())
        check(nvme0 not in volume("tank/rest")["alloc"], volume("tank/rest"))
        # The cherry user's home is made, if it is not there, owned by that user.
        home = f"{tmp()}/home"
        check(os.stat(home).st_uid == os.getuid() and os.stat(f"{home}/volumes").st_mode & 0o777 == 0o755, "home")

    @case("cache: every volume cached on the cache disks, in proportion to its share of the data disks")
    def _():
        sda, sdb, sdc, nvme0, nvme1 = setup()
        m.run("create:tank:sda:sdb")
        m.run("volume-create:tank:big:100G")
        m.run("volume-create:tank:small:20G")
        expect("cache", m.run("cache:tank:writethrough:nvme0n1:nvme1n1"), 0,
               f"the pool tank is cached on {nvme0} {nvme1}, writethrough")
        # 200 GiB of data disks, 20 GiB of cache disks (5120 extents): a tenth of each volume.
        calls = m.calls()
        check(calls[:4] == [f"lvm vgextend tank {nvme0}", f"lvm pvchange --addtag cache {nvme0}",
                            f"lvm vgextend tank {nvme1}", f"lvm pvchange --addtag cache {nvme1}"], calls)
        check(f"lvm lvcreate -y {POOL} -n big_cache -l 2548 tank {nvme0} {nvme1}" in calls
              and "lvm lvconvert -y --type cache --cachepool tank/big_cache --cachemode writethrough tank/big" in calls
              and f"lvm lvcreate -y {POOL} -n small_cache -l 500 tank {nvme0} {nvme1}" in calls, calls)
        check(volume("tank/big")["segtype"] == "cache" and volume("tank/[big_cache_cpool]")["hidden"], m.state()["lvs"])
        # A new volume is cached too, in the same proportion; writeback once the pool is.
        expect("writeback", m.run("cache:tank:writeback:nvme0n1:nvme1n1"), 0, "cached on")
        calls = m.calls()
        check(calls[:2] == ["lvm lvconvert -y --uncache tank/big", "lvm lvconvert -y --uncache tank/small"]
              and "lvm vgchange --addtag writeback tank" in calls
              and "lvm lvconvert -y --type cache --cachepool tank/big_cache --cachemode writeback tank/big" in calls,
              calls)
        m.run("volume-create:tank:new:40G")
        check(f"lvm lvcreate -y {POOL} -n new_cache -l 1012 tank {nvme0} {nvme1}" in m.calls()
              and volume("tank/new")["mode"] == "writeback", m.calls())
        # Too small a share for a cache: none.
        rc, out = m.run("volume-create:tank:tiny:100M")
        check(rc == 0 and "no cache for tank/tiny" in out and volume("tank/tiny")["segtype"] == "linear", out)
        # One cache disk fewer, and writethrough again: the other one leaves the pool.
        expect("fewer", m.run("cache:tank:writethrough:nvme0n1"), 0,
               f"the pool tank is cached on {nvme0}, writethrough")
        calls = m.calls()
        check(f"lvm vgreduce tank {nvme1}" in calls and "lvm vgchange --deltag writeback tank" in calls
              and f"lvm lvcreate -y {POOL} -n big_cache -l 1268 tank {nvme0}" in calls, calls)
        check(m.state()["pvs"][nvme1]["vg"] == "", "the cache disk left is still in the pool")
        # A data disk is not a cache disk, nor is another pool's.
        expect("data as cache", m.run("cache:tank:writethrough:sda"), 1, "/dev/sda is in the pool tank")
        expect("bad mode", m.run("cache:tank:fast:nvme0n1"), 1, "bad cache mode 'fast'", [])
        expect("none", m.run("cache:tank:writethrough"), 0, "the pool tank has no cache")
        check(all(lv["segtype"] == "linear" for lv in m.state()["lvs"].values()) and
              m.state()["pvs"][nvme0]["vg"] == "", m.state())

    @case("resize: grows the volume and its filesystem, its cache detached meanwhile")
    def _():
        sda, sdb, _, nvme0, _ = setup()
        m.run("create:tank:sda:sdb")
        m.run("volume-create:tank:web:50G")
        m.run("cache:tank:writethrough:nvme0n1")
        expect("grow", m.run("volume-resize:tank:web:80G"), 0, "grew the volume tank/web from 51200 MiB to 81920 MiB",
               None)
        calls = m.calls()
        check(calls[:3] == ["lvm lvconvert -y --uncache tank/web", f"lvm lvextend -L 80G tank/web {sda} {sdb}",
                            f"btrfs filesystem resize max {tmp()}/home/volumes/tank/web"]
              and volume("tank/web")["segtype"] == "cache", calls)
        expect("shrink", m.run("volume-resize:tank:web:10G"), 1, "volumes only grow")
        check(volume("tank/web")["segtype"] == "cache", "the cache did not come back after a failed resize")
        expect("all", m.run("volume-resize:tank:web:all"), 0, "to 204800 MiB")
        expect("no volume", m.run("volume-resize:tank:nope:1G"), 1, "no volume nope in the pool tank", [])

    @case("reduce: a data disk's data moves to the other data disks, never to the cache disks")
    def _():
        sda, sdb, sdc, nvme0, _ = setup()
        m.run("create:tank:sda:sdb")
        m.run("volume-create:tank:web:150G")
        m.run("cache:tank:writethrough:nvme0n1")
        expect("no room", m.run("reduce:tank:sda"), 1,
               "the pool tank's other data disks have 51200 MiB free, and /dev/sda holds 102400 MiB: make room first",
               [])
        m.run("extend:tank:sdc")
        expect("reduce", m.run("reduce:tank:sda"), 0, "removed /dev/sda from the pool tank: it is still claimed")
        calls = m.calls()
        check(calls[0] == "lvm lvconvert -y --uncache tank/web" and f"lvm pvmove {sda} {sdb} {sdc}" in calls
              and calls[-1] == f"lvm vgreduce tank {sda}", calls)
        check(sda not in volume("tank/web")["alloc"] and volume("tank/web")["segtype"] == "cache", volume("tank/web"))
        check(m.state()["pvs"][sda]["vg"] == "", "sda is still in the pool")
        # An empty data disk leaves at once; the last one does not.
        m.run("extend:tank:sda")
        expect("empty", m.run("reduce:tank:sda"), 0, "removed /dev/sda", [f"lvm vgreduce tank {sda}"])
        m.run("volume-remove:tank:web")
        m.run("reduce:tank:sdb")
        expect("last", m.run("reduce:tank:sdc"), 1, "/dev/sdc is the last data disk of the pool tank", [])
        expect("not in it", m.run("reduce:tank:sda"), 1, "/dev/sda is not in the pool tank", [])
        # A cache disk: as cache without it.
        expect("cache disk", m.run("reduce:tank:nvme0n1"), 0, "removed /dev/nvme0n1", [f"lvm vgreduce tank {nvme0}"])

    @case("reduce: a failed pvmove brings the caches back")
    def _():
        setup()
        m.run("create:tank:sda:sdb")
        m.run("volume-create:tank:web:150G")
        m.run("cache:tank:writethrough:nvme0n1")
        m.run("extend:tank:sdc")
        m.change(fail=["pvmove"])
        expect("pvmove fails", m.run("reduce:tank:sda"), 1, "cannot move the data off /dev/sda")
        m.change(fail=[])
        check(volume("tank/web")["segtype"] == "cache", "the cache did not come back")

    @case("destroy, remove, release: nothing in use goes")
    def _():
        sda, sdb, _, _, _ = setup()
        m.run("create:tank:sda:sdb")
        m.run("volume-create:tank:web:10G")
        mp = f"{tmp()}/home/volumes/tank/web"
        expect("destroy with volumes", m.run("destroy:tank"), 1,
               "the pool tank still has volumes: web: remove them first", [])
        m.change(busy=[mp])
        expect("busy", m.run("volume-remove:tank:web"), 1, f"cannot unmount {mp}: in use")
        check("tank/web" in m.state()["lvs"], "a busy volume was removed")
        m.change(busy=[])
        expect("remove", m.run("volume-remove:tank:web"), 0, "removed the volume tank/web",
               [f"umount {mp}", "lvm lvremove -y tank/web"])
        check(not os.path.exists(mp), "the mount point is left")
        expect("release in a pool", m.run(f"release:sda:{m.fingerprint('sda')}"), 1, "/dev/sda is in the pool tank", [])
        expect("destroy", m.run("destroy:tank"), 0, "destroyed the pool tank: its disks are still claimed",
               ["lvm vgremove -y -f tank"])
        expect("stale", m.run("release:sda:0123456789ab"), 1, "/dev/sda is not as it was listed", [])
        expect("release", m.run(f"release:sda:{m.fingerprint('sda')}"), 0, "released /dev/sda: it is blank",
               [f"lvm pvremove -y {sda}", "systemd-cryptsetup detach cherry-pv-sda1", "wipefs --all /dev/sda1",
                "wipefs --all /dev/sda"])
        sda_now = next(d for d in m.list()["disks"] if d["name"] == "sda")
        check(sda_now["state"] == "free" and sda_now["signatures"] == [] and sda_now["partitions"] == [], sda_now)
        expect("release unclaimed", m.run(f"release:sda:{sda_now['fingerprint']}"), 1, "/dev/sda is not claimed", [])

    @case("one operation at a time")
    def _():
        setup()
        with open(f"{tmp()}/lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            expect("locked", m.run("create:tank:sda"), 1, "another storage operation is running", [])
            check(m.run("list")[0] == 0, "list waits for the lock")

    @case("boot: unlocks, brings up /var, activates the pools and mounts the volumes; the reverse at shutdown")
    def _():
        sda, sdb, _, _, _ = setup()
        m.run("create:tank:sda:sdb")
        m.run("volume-create:tank:web:10G")
        m.run("volume-create:tank:db:10G")
        vols = f"{tmp()}/home/volumes/tank"
        # Shutdown: systemd unmounts the volumes and /var, then every pool is deactivated and every disk closed; a
        # volume still mounted is unmounted first.
        m.unmount_var()
        m.run("boot", "stop")
        calls = m.calls()
        check(calls[:4] == [f"umount {vols}/web", f"umount {vols}/db", "lvm vgchange -an cherry",
                            "lvm vgchange -an tank"]
              and sorted(calls[4:]) == [f"systemd-cryptsetup detach cherry-pv-{p}"
                                        for p in ("nvme0n1p1", "nvme1n1p1", "sda1", "sdb1", "sdc1")], calls)
        # The next boot: the disks unlocked, /var first (in RAM again: the pool cherry was never given disks), then
        # the volumes, as they are. sdc's volume belongs to another TPM now, and db will not mount: the rest goes on.
        m.reboot()
        m.change(locked=["sdc1"], nomount=[f"{vols}/db"])
        rc, out = m.run("boot", "start")
        check(rc == 1 and "the TPM cannot unlock /dev/sdc1: another machine's disk?" in out
              and f"128M in RAM on {ZRAM}" in out and "cannot mount tank/db" in out, out)
        calls = m.calls()
        check([c for c in calls if c.startswith("systemd-cryptsetup")] == [
            f"systemd-cryptsetup attach cherry-pv-{p} /dev/{p} - tpm2-device=auto,headless"
            for p in ("nvme0n1p1", "nvme1n1p1", "sda1", "sdb1", "sdc1")], calls)
        check(calls[-3:] == ["lvm vgchange -ay tank", f"mount -t btrfs -o nodev,nosuid /dev/tank/db {vols}/db",
                             f"mount -t btrfs -o nodev,nosuid /dev/tank/web {vols}/web"], calls)
        check("lvm vgchange -ay cherry" not in calls, calls)
        # Again: /var is there, what is mounted is left alone, what is unlocked too.
        m.change(locked=[], nomount=[])
        rc, out = m.run("boot", "start")
        calls = m.calls()
        check(rc == 0 and "/var is mounted already" in out and not [c for c in calls if c.endswith("/web")]
              and f"mount -t btrfs -o nodev,nosuid /dev/tank/db {vols}/db" in calls
              and [c for c in calls if c.startswith("systemd-cryptsetup")] == [
                  "systemd-cryptsetup attach cherry-pv-sdc1 /dev/sdc1 - tpm2-device=auto,headless"], out + str(calls))
        # A pool that does not come up: its volumes are not mounted; /var is.
        m.shutdown()
        m.change(fail=["vgchange"])
        rc, out = m.run("boot", "start")
        m.change(fail=[])
        check(rc == 1 and "the pool tank is missing disks: not all its volumes are there" in out
              and "tank/web is not active: not mounted" in out and not [c for c in m.calls() if "/tank/" in c], out)
        check(m.list()["var"]["ram"] is True, "/var did not come up")
        expect("bad boot verb", m.run("boot", "restart"), 1, "bad boot verb 'restart'")

    @case("/var: without the pool, boot start makes it in RAM, a zram device with var of 128 MiB, btrfs")
    def _():
        m.fresh(ram_var=False)
        rc, out = m.run("boot", "start")
        check(rc == 0 and f"/var is cherry/var (btrfs), 128M in RAM on {ZRAM}: nothing persists until the pool has "
              "disks" in out, out)
        check(m.calls() == [f"lvm pvcreate -y {ZRAM}", f"lvm vgcreate --setautoactivation n cherry {ZRAM}",
                            f"lvm pvchange --addtag ram {ZRAM}", "lvm lvcreate -y -n var -L 128M cherry",
                            "mkfs.btrfs -q /dev/mapper/cherry-var",
                            "mount -t btrfs -o nodev,discard=async /dev/mapper/cherry-var /var"], m.calls())
        check(read("zram1", "comp_algorithm") == "zstd" and int(read("zram1", "disksize")) == 132 * MIB, "zram1")
        check(m.state()["pvs"] == {ZRAM: {"vg": "cherry", "tags": ["ram"], "extents": 32}}, m.state()["pvs"])
        check(volume("cherry/var")["alloc"] == {ZRAM: 32}, volume("cherry/var"))
        check(m.list()["var"] == {"fstype": "btrfs", "source": "/dev/mapper/cherry-var", "ram": True}, m.list()["var"])
        # Again: nothing to do.
        expect("mounted", m.run("boot", "start"), 0, "/var is mounted already", [])
        # What fails is undone, and /var is the tmpfs of var.mount, in RAM too as list sees it.
        m.fresh(ram_var=False)
        m.change(fail=["lvcreate"])
        rc, out = m.run("boot", "start")
        check(rc == 1 and f"cannot make the pool cherry in RAM on {ZRAM}: /var is a tmpfs" in out, out)
        check(m.calls()[-2:] == ["lvm vgremove -y -f cherry", f"lvm pvremove -y {ZRAM}"]
              and m.state()["vgs"] == {} and m.state()["pvs"] == {}, m.calls())
        check(read("zram1", "reset") == "1" and m.zram_control("hot_remove") == "1", "the zram device was not reset")
        check(not [c for c in m.calls() if c.startswith("mount")], m.calls())
        open(f"{tmp()}/mountinfo", "w").write("36 25 0:32 / /var rw,nodev,relatime shared:5 - tmpfs tmpfs rw\n")
        check(m.list()["var"] == {"fstype": "tmpfs", "source": "tmpfs", "ram": True}, m.list()["var"])
        m.change(fail=[])

    @case("/var: nothing but extend touches the pool in RAM; extend moves /var onto the disks, the zram device goes")
    def _():
        sda, sdb, _, nvme0, _ = setup()
        for instance, message in [
                ("create:cherry:sda", "the pool cherry is the machine's own, there from boot: extend it"),
                ("destroy:cherry", "the pool cherry holds /var: it is never destroyed"),
                ("volume-remove:cherry:var", "cherry/var is /var: it is never removed"),
                ("volume-create:cherry:data:1G", "the pool cherry is in RAM: give it disks first (extend)"),
                ("volume-resize:cherry:var:4G", "the pool cherry is in RAM: give it disks first (extend)"),
                ("cache:cherry:writethrough:nvme0n1", "the pool cherry is in RAM: give it disks first (extend)")]:
            expect(instance, m.run(instance), 1, message, [])
        expect("extend", m.run("extend:cherry:sda:sdb"), 0,
               "/var is on the pool's disks now, and persists; its volume var is 128M: grow it (volume-resize)",
               [f"lvm vgextend cherry {sda} {sdb}", f"lvm pvchange --addtag data {sda} {sdb}",
                f"lvm pvmove {ZRAM} {sda} {sdb}", f"lvm vgreduce cherry {ZRAM}", f"lvm pvremove -y {ZRAM}"])
        check(volume("cherry/var")["alloc"] == {sda: 32} and ZRAM not in m.state()["pvs"], m.state())
        check(read("zram1", "reset") == "1" and m.zram_control("hot_remove") == "1", "the zram device was not freed")
        facts = m.list()
        check(facts["var"] == {"fstype": "btrfs", "source": "/dev/mapper/cherry-var", "ram": False}, facts["var"])
        check(next(p for p in facts["pvs"]["report"][0]["pv"] if p["pv_name"] == sda)["pv_tags"] == "data",
              facts["pvs"])
        # Then var grows, and its btrfs with it; the pool takes volumes and a cache, which caches var too.
        expect("grow var", m.run("volume-resize:cherry:var:4G"), 0,
               "grew the volume cherry/var from 128 MiB to 4096 MiB",
               [f"lvm lvextend -L 4G cherry/var {sda} {sdb}", "btrfs filesystem resize max /var"])
        expect("var again", m.run("volume-create:cherry:var:1G"), 1, "cherry/var is /var: it is there already", [])
        expect("volume", m.run("volume-create:cherry:data:1G"), 0,
               f"created the volume cherry/data, mounted on {tmp()}/home/volumes/cherry/data")
        expect("cache", m.run("cache:cherry:writethrough:nvme0n1"), 0, f"the pool cherry is cached on {nvme0}")
        # var's share of the cache is worth a cache; the 1 GiB volume's is under the minimum.
        check(volume("cherry/var")["segtype"] == "cache" and volume("cherry/data")["segtype"] == "linear",
              m.state()["lvs"])
        # Its last data disk stays.
        expect("empty disk out", m.run("reduce:cherry:sdb"), 0, "removed /dev/sdb from the pool cherry")
        expect("last", m.run("reduce:cherry:sda"), 1,
               "/dev/sda is the last data disk of the pool cherry, which holds /var: it stays", [])

    @case("/var: a move that fails leaves the pool in RAM, and the disks claimed, in no pool")
    def _():
        sda, _, _, _, _ = setup()
        m.change(fail=["pvmove"])
        expect("pvmove fails", m.run("extend:cherry:sda"), 1,
               "cannot move /var onto the disks: the pool cherry is as it was, in RAM")
        m.change(fail=[])
        check(m.calls()[-3:] == [f"lvm pvmove {ZRAM} {sda}", f"lvm pvmove --abort {ZRAM}",
                                 f"lvm vgreduce cherry {sda}"], m.calls())
        check(m.state()["pvs"][sda]["vg"] == "" and volume("cherry/var")["alloc"] == {ZRAM: 32}, m.state())
        check(m.list()["var"]["ram"] is True, "var is not in RAM")

    @case("/var: at boot with the pool on its disks, unlocked, activated and mounted before anything else")
    def _():
        sda, sdb, sdc, _, _ = setup()
        m.run("extend:cherry:sda:sdb")
        m.run("create:tank:sdc")
        m.run("volume-create:tank:web:10G")
        vols = f"{tmp()}/home/volumes/tank"
        m.unmount_var()
        m.run("boot", "stop")
        check([c for c in m.calls() if c.startswith("lvm")] == ["lvm vgchange -an cherry", "lvm vgchange -an tank"]
              and len([c for c in m.calls() if c.startswith("systemd-cryptsetup detach")]) == 5, m.calls())
        m.reboot()
        rc, out = m.run("boot", "start")
        check(rc == 0 and "/var is cherry/var (btrfs) on the pool's disks, unlocked with the TPM" in out, out)
        calls = m.calls()
        check(len([c for c in calls if c.startswith("systemd-cryptsetup attach")]) == 5
              and [c for c in calls if not c.startswith("systemd-cryptsetup")] == [
                  "lvm vgchange -ay cherry", "mount -t btrfs -o nodev /dev/mapper/cherry-var /var",
                  "lvm vgchange -ay tank", f"mount -t btrfs -o nodev,nosuid /dev/tank/web {vols}/web"], calls)
        check(not [c for c in calls if "zram" in c], f"with the pool on disk: {calls}")
        # Disks another TPM claimed, this one's before it was cleared say: the pool is not seen, and /var is in RAM
        # again, a new pool cherry; the disks are left as they are.
        m.shutdown()
        m.change(locked=["sda1", "sdb1"])
        rc, out = m.run("boot", "start")
        check(rc == 0 and "the TPM cannot unlock /dev/sda1" in out and "the TPM cannot unlock /dev/sdb1" in out
              and f"128M in RAM on {ZRAM}" in out, out)
        check(not [c for c in m.calls() if c.startswith("wipefs")]
              and f"mount -t btrfs -o nodev,nosuid /dev/tank/web {vols}/web" in m.calls(), m.calls())
        m.change(locked=[])

    @case("/var: a missing disk that no volume uses goes; one a volume uses leaves /var a tmpfs and the disks alone")
    def _():
        sda, sdb, _, _, _ = setup()
        m.run("extend:cherry:sda:sdb")
        # A crash between the move off the zram device and its removal: a missing physical volume, unused, at the
        # next boot.
        m.shutdown()
        state = m.state()
        state["pvs"][ZRAM] = {"vg": "cherry", "tags": ["ram"], "extents": 32}
        save(state)
        shutil.rmtree(block("zram1"))
        rc, out = m.run("boot", "start")
        check(rc == 0 and "on the pool's disks" in out, out)
        check("lvm vgreduce --removemissing cherry" in m.calls() and ZRAM not in m.state()["pvs"], m.calls())
        # var grown over both disks, and sdb another TPM's now: var is not whole, so /var is a tmpfs for the boot,
        # and nothing is removed or written.
        m.run("volume-resize:cherry:var:150G")
        check(sdb in volume("cherry/var")["alloc"], volume("cherry/var"))
        m.shutdown()
        m.change(locked=["sdb1"])
        rc, out = m.run("boot", "start")
        m.change(locked=[])
        check(rc == 1 and "the pool cherry is missing a disk that its volumes use" in out
              and "/var is a tmpfs, and no volume is mounted" in out, out)
        calls = m.calls()
        check(calls[-3:] == ["lvm vgchange -ay cherry", "lvm vgreduce --removemissing cherry",
                             "lvm vgchange -an cherry"]
              and not [c for c in calls if c.startswith(("mount", "wipefs", "mkfs"))], calls)
        check(sdb in m.state()["pvs"] and sdb in volume("cherry/var")["alloc"], "the missing disk was given up")

    @case("list: pools and their volumes, mounted, from LVM's reports")
    def _():
        setup()
        m.run("create:tank:sda:sdb")
        m.run("cache:tank:writeback:nvme0n1")
        m.run("volume-create:tank:web:10G")
        facts = m.list()
        names = [p["pv_name"] for p in facts["pvs"]["report"][0]["pv"]]
        check(names[:2] == ["/dev/mapper/cherry-pv-nvme0n1p1", "/dev/mapper/cherry-pv-nvme1n1p1"], facts)
        check([v["vg_name"] for v in facts["vgs"]["report"][0]["vg"]] == ["cherry", "tank"], facts["vgs"])
        check(sorted(lv["lv_name"] for lv in facts["lvs"]["report"][0]["lv"]) == ["[web_cache_cpool]", "var", "web"],
              facts["lvs"])
        check([mp["path"] for mp in facts["mounts"]] == ["/var", f"{tmp()}/home/volumes/tank/web"]
              and facts["mounts"][1] == {"path": f"{tmp()}/home/volumes/tank/web", "size": 10737418240,
                                         "used": 1073741824, "available": 9663676416}, facts["mounts"])


def tmp():
    return m.tmp


# What makes up the stand-in machine, in its directory.
MACHINE = ["sys", "udev", "identity", "home", "logs", "mountinfo", "swaps", "state"]


def copy(source, target):
    if os.path.isdir(source):
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy(source, target)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in STANDINS:
        standin(sys.argv[1], sys.argv[2:])
    else:
        main()
