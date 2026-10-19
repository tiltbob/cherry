#!/usr/bin/env python3
"""End-to-end smoke test for Cherry: UEFI HTTP boot in QEMU + OVMF (stdlib only).

  1. the firmware HTTP boots the EFI binary; the root filesystem is the EROFS
     image from the initramfs, read-only; /var is the volume var of the pool
     cherry, the machine's own, which is in RAM (a zram device of 132 MiB,
     var 128 MiB, btrfs) as the pool has no disks yet, and the machine's blank
     disk is left alone; the machine ID comes from the TPM's endorsement key
     (a swtpm on CRB), and the hostname from the machine ID; networking works,
     no unit failed; the kernel is locked down in integrity mode
     and loads only the modules the build signed: every module the image
     carries is, one of them loads, and a copy of it without its signature is
     refused; the firewall was
     loaded before networkd started, lets IPv6 router advertisements in, and
     keeps a service listening on the host from answering outside the VM until
     a rule lets it in, which restarting the firewall takes back out
  2. through OpenCode's storage MCP server, as the cherry user, the agent
     claims the blank disk, now a LUKS2 volume whose key the TPM seals to the
     boot state (PCR 7, and PCR 11 under the image's PCR key, as the header's
     token shows; the image's policy is signed, and PCR 11 is what it says),
     and gives it to the pool cherry: /var moves onto it live, with
     cherry.service, its OpenChamber and the files written before untouched,
     and the zram device goes; var then grows to 4 GiB, and /var with it
  3. a systemd-nspawn container runs with a veth link, and systemd-networkd
     sets up masquerading for it and can write its state files, even after
     systemd-tmpfiles ran in an arch-chroot of a root that numbers its users
     differently (as pacstrap's pacman hooks do); through the firewall, the
     container leases its address from networkd, pings the host and reaches
     out of the VM, but a service listening on the host does not answer it
  4. after a poweroff, which unmounts /var cleanly, a second boot (the same
     TPM, now on TIS) unlocks the disk, whose key stays sealed as it was,
     mounts var and finds the files, with
     the same machine ID and hostname; machinectl clone on it makes a snapshot
     of an image, as environments are; another TPM cannot unlock the disk, so
     that machine's /var is in RAM, nothing claims the disk, and it stays as
     it was; back on its own TPM, everything is there
  5. the cherry user creates a Debian container with cherry-bootstrap@.service
     (debootstrap, as root), boots, stops and removes it with machinectl
     (skipped when the guest cannot reach deb.debian.org)
  6. rpmstrap installs Fedora into /var/lib/machines and the container boots
     (skipped when the guest cannot reach mirrors.fedoraproject.org)
  7. pacstrap installs Arch Linux into /var/lib/machines, the container's own
     pacman downloads in its Landlock sandbox, and the container boots
     (skipped when the guest cannot reach the Arch Linux mirrors)
  8. the OpenCode binary is the packaged version (stripped, it would be a bare
     Bun), and only the binary: no opencode user, state directory or service
  9. cherry.service runs OpenChamber on Bun as the cherry user, serving its web
     UI and running the packaged OpenCode as that user too, both on loopback
     only and under its system call filter; once it was online,
     cherry-connect-url.service showed the pairing link and its QR code on the
     console, as that user; the user's polkit privileges cover machinectl and
     cherry-bootstrap@ and nothing else; no openchamber user, state directory
     or service
  10. OpenCode reads its system configuration, /etc/opencode, which denies the
     agent every tool but the MCP servers'; through the machines server, as
     the cherry user in cherry.service's sandbox, a Debian image is built from
     a profile of the machine's own (cherry-image@.service) and refused a
     start, an environment is cloned from it with its settings, started with
     its network up, read with status, stopped, enabled to start at boot,
     which the next boot does (cherry-autostart.service, before
     machines.target, from the record on the machine's disk), disabled, which
     stops it and leaves the unit nothing to start, and removed before the
     image is; OpenChamber lists the Environments extension as a granted
     built-in, whose service answers in the same sandbox; cherry-image@
     writes nothing out, as images never leave the machine (skipped when the
     guest cannot reach deb.debian.org)
  11. zram is the swap device, and under memory pressure tmpfs pages go to it,
     compressed, and come back intact
  12. without a TPM, boot stops at cherry-no-tpm.target with the error on the
     console: no network, no OpenChamber, no containers
  13. on Secure Boot firmware in setup mode, cherry-secureboot.service enrolls
     the image's keys and the option ROM driver the firmware loaded (QEMU's
     iPXE for virtio-net) and reboots; the firmware then refuses the image,
     unsigned, but boots it with Secure Boot on once signed by a CA the test
     adds to db; an image the build signed with a chain of its own (README,
     Secure Boot) boots as it is (skipped when the image has no keys)
  14. storage, through OpenCode's storage MCP server as the cherry user, with
     the pool cherry on the machine's disk: claim refuses a disk not as
     listed, then wipes a blank hard disk, one with a partition table and an
     NVMe SSD with one too, each now a LUKS2 volume the TPM unlocks, with
     cherry's the only physical volumes LVM sees; the pool cherry is cached on
     the SSD, var with it, and is never destroyed, var never removed, its disk
     never taken out; a pool of the hard disks cached on the SSD, a volume in
     it, cached, mounted, the cherry user's, which cherry.service sees
     writable; a hard disk out of the pool, its data moved, and the volume
     grown; after a poweroff, which closes it all cleanly, the next boot
     unlocks, activates and mounts it all again; then the cache, the volume
     and the pool go, and the disks are released blank
  15. the disks' keys are bound to the boot state: with /var on a disk the
     agent gave the pool cherry, sealed to the image's policy, the TPM does
     not unlock it under the same image with one byte of its command line
     changed, which PCR 11 shows, nor under the image signed anew with
     another PCR key: the pool is not seen, /var is in RAM, and the disk is
     left as it was; under the image itself it does, and the files are there
  16. the prod image, cherry-x86_64-prod.efi, the same build with cherry.prod
     on its command line: it boots to the pairing link on the console, and the
     console refuses root, which has no password that would do, without
     asking for one, with the agetty.autologin credential as without it, where
     the dev image, which every other scenario drives through it, logs root in

The scenarios are Smoke's s<N>_ methods, run in the order of N, which must be
1, 2, ... with no gap or duplicate (tests/scenarios.py checks that too).
Timeouts scale with CHERRY_TEST_TIMEOUT_MULT (default 1 with KVM, 4 without).
"""

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import pcrsign  # noqa: E402
import run_qemu  # noqa: E402
import secureboot  # noqa: E402

MULT = float(os.environ.get("CHERRY_TEST_TIMEOUT_MULT", "1" if run_qemu.kvm_usable() else "4"))

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|[=>78cDEHM])")
BOOT = re.compile(r'BdsDxe: starting Boot([0-9A-F]{4}) "([^"]*)"')
PANIC = re.compile(r"Kernel panic - not syncing")
# DER SubjectPublicKeyInfo of an uncompressed NIST P-256 public key, up to the point.
P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d03010703420004")
# systemd's key for the ?s of /etc/hostname (hostname_substitute_wildcards() in src/shared/hostname-setup.c).
HOSTNAME_KEY = bytes.fromhex("9810addf8d7d4fb5891b4b56acc2268f")
NO_TPM_BANNER = "Cherry stopped: no usable TPM 2.0."
# Run a command as the cherry user, from the root shell on the console.
AS_CHERRY = "systemd-run -q --wait --pipe --collect -p User=cherry"
# As cherry.service runs OpenCode, and the MCP servers it starts: the parts of its sandbox that could stop them.
IN_CHERRY_SERVICE = ("-E HOME=/var/lib/cherry -p NoNewPrivileges=yes -p ProtectSystem=strict "
                     "-p ReadWritePaths=/var/lib/cherry -p PrivateTmp=yes -p PrivateDevices=yes "
                     "-p CapabilityBoundingSet= -p RestrictNamespaces=yes "
                     "-p 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK' "
                     "-p SystemCallFilter=@system-service -p 'SystemCallFilter=~@privileged' "
                     "-p SystemCallErrorNumber=EPERM")
# The probe (Smoke.start_probe): a service of the host's listening on this port, which the firewall keeps from
# answering outside the VM and the containers; QEMU forwards a host port to it.
PROBE_PORT = 2222
PROBE = "smoke-probe"
# A DOS partition table with one Linux partition, of 2 MiB from 1 MiB on: a disk in use, not a blank one.
DOS_MBR = (bytes(446) + bytes([0, 0, 0, 0, 0x83, 0, 0, 0]) + (2048).to_bytes(4, "little")
           + (4096).to_bytes(4, "little") + bytes(48) + b"\x55\xaa")
FATAL = [
    (re.compile(r"No bootable option"), "the firmware found nothing to boot"),
    (re.compile(r'BdsDxe: starting Boot[0-9A-F]{4} "EFI Internal Shell'),
     "the firmware fell through to the UEFI shell"),
    (re.compile(r"^Shell> ", re.M), "the firmware started the UEFI shell"),
    (re.compile(r"Found ordering cycle"), "systemd found an ordering cycle"),
    (PANIC, "kernel panic"),
]


SKIPPED = "skipped"
SCENARIO = re.compile(r"s(\d+)_")


def scenario_names(cls):
    """The names of cls's s<N>_ methods in the order of N, which must be 1, 2, ... with no gap or duplicate."""
    found = {}
    for name in dir(cls):
        m = SCENARIO.match(name)
        if m and callable(getattr(cls, name)):
            found.setdefault(int(m[1]), []).append(name)
    problems = [f"{' and '.join(names)} share the number {n}" for n, names in sorted(found.items()) if len(names) > 1]
    problems += [f"no s{n}_ scenario" for n in range(1, max(found, default=0) + 1) if n not in found]
    problems += [f"{found[n][0]} is numbered below 1" for n in sorted(found) if n < 1]
    check(not problems, "the s<N>_ scenarios must be numbered 1, 2, ... with no gap or duplicate: "
          + "; ".join(problems))
    return [found[n][0] for n in sorted(found)]


def tcp_local_address(hexaddr):
    """Decode a local address of /proc/net/tcp or tcp6 ("0100007F:0BB8") into (ip, port)."""
    addr, port = hexaddr.split(":")
    raw = bytes.fromhex(addr)
    # Each 32-bit word is in host byte order: little-endian on x86.
    words = [raw[i:i + 4][::-1] for i in range(0, len(raw), 4)]
    return str(ipaddress.ip_address(b"".join(words))), int(port, 16)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def free_port():
    """A TCP port on 127.0.0.1 that nothing listens on (yet)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def package_version(name):
    """The version of one of Cherry's packages, from package/<name>/<name>.mk."""
    mk = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "package", name, f"{name}.mk")
    with open(mk) as f:
        return re.search(rf"^{name.upper()}_VERSION = (\S+)$", f.read(), re.M).group(1)


def siphash24(key, data):
    """SipHash-2-4 of data with a 16-byte key, as systemd's siphash24()."""
    mask = (1 << 64) - 1

    def rotl(x, b):
        return (x << b | x >> (64 - b)) & mask

    def rounds(v, n):
        v0, v1, v2, v3 = v
        for _ in range(n):
            v0 = (v0 + v1) & mask
            v1 = rotl(v1, 13) ^ v0
            v0 = rotl(v0, 32)
            v2 = (v2 + v3) & mask
            v3 = rotl(v3, 16) ^ v2
            v0 = (v0 + v3) & mask
            v3 = rotl(v3, 21) ^ v0
            v2 = (v2 + v1) & mask
            v1 = rotl(v1, 17) ^ v2
            v2 = rotl(v2, 32)
        return [v0, v1, v2, v3]

    k0, k1 = int.from_bytes(key[:8], "little"), int.from_bytes(key[8:], "little")
    v = [k0 ^ 0x736f6d6570736575, k1 ^ 0x646f72616e646f6d, k0 ^ 0x6c7967656e657261, k1 ^ 0x7465646279746573]
    # Little-endian 64-bit words; the last one holds the leftover bytes and, in its top byte, the length.
    end = len(data) - len(data) % 8
    words = [int.from_bytes(data[i:i + 8], "little") for i in range(0, end, 8)]
    words.append((len(data) & 0xff) << 56 | int.from_bytes(data[end:], "little"))
    for m in words:
        v[3] ^= m
        v = rounds(v, 2)
        v[0] ^= m
    v[2] ^= 0xff
    v = rounds(v, 4)
    return v[0] ^ v[1] ^ v[2] ^ v[3]


def hostname_from_machine_id(pattern, machine_id):
    """The hostname systemd sets from an /etc/hostname pattern: each ? becomes the next hex digit, low nibble first,
    of SipHash-2-4 keyed with HOSTNAME_KEY over the machine ID and a counter (64-bit, little-endian) from 0."""
    mid = bytes.fromhex(machine_id)
    name, h, left, counter = "", 0, 0, 0
    for c in pattern:
        if c == "?":
            if left == 0:
                h, left = siphash24(HOSTNAME_KEY, mid + counter.to_bytes(8, "little")), 64
                counter += 1
            c = "0123456789abcdef"[h & 0xf]
            h >>= 4
            left -= 4
        name += c
    return name


class TestFailure(Exception):
    pass


class Timeout(TestFailure):
    pass


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(condition, message):
    if not condition:
        raise TestFailure(message)


class Console:
    """Serial console reader: strips ANSI escapes and CRs, logs raw output."""

    def __init__(self, proc, log_path):
        self.proc = proc
        self.log = open(log_path, "ab")
        self.buf = ""
        self.pos = 0
        self.eof = False
        self.cond = threading.Condition()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        fd = self.proc.stdout.fileno()
        pending = ""
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            self.log.write(data)
            self.log.flush()
            text = pending + data.decode("utf-8", "replace")
            pending = ""
            cut = text.rfind("\x1b")
            # ESC \ ends an OSC (e.g. systemd 258's OSC 3008 context sequences):
            # judge completeness from the ESC that starts it.
            if cut > 0 and text.startswith("\x1b\\", cut):
                cut = text.rfind("\x1b", 0, cut)
            if cut != -1 and not ANSI.match(text, cut) and len(text) - cut < 64:
                text, pending = text[:cut], text[cut:]
            clean = ANSI.sub("", text).replace("\r", "")
            with self.cond:
                self.buf += clean
                self.cond.notify_all()
        with self.cond:
            self.eof = True
            self.cond.notify_all()

    def expect(self, pattern, timeout, allow_boot=False):
        """Wait for pattern; return (match, text before the match)."""
        if isinstance(pattern, str):
            pattern = re.compile(pattern, re.M)
        deadline = time.monotonic() + timeout * MULT
        with self.cond:
            while True:
                window = self.buf[self.pos:]
                m = pattern.search(window)
                seen = window[: m.end()] if m else window
                for fatal, reason in FATAL:
                    if fatal.search(seen):
                        raise TestFailure(f"{reason}:\n{seen[-3000:]}")
                if not allow_boot and BOOT.search(seen):
                    raise TestFailure(f"unexpected reboot:\n{seen[-3000:]}")
                if m:
                    self.pos += m.end()
                    return m, window[: m.start()]
                if self.eof:
                    raise TestFailure(f"QEMU exited while waiting for {pattern.pattern!r}:\n{window[-3000:]}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Timeout(f"timed out waiting for {pattern.pattern!r}; last output:\n{window[-3000:]}")
                self.cond.wait(min(remaining, 1.0))


class VM:
    def __init__(self, workdir, server):
        self.workdir = workdir
        self.server = server
        self.proc = None
        self.tpm = None
        self.console = None
        self.probe_port = None
        self.n = 0

    def start(self, tpm_interface="crb", secure_boot=False, server=None, tpm="tpm", disks=(), ssds=(),
              name=run_qemu.EFI_NAME, credentials=None):
        """Boot the image `name` with a TPM on tpm_interface, or None for no TPM, from server (default: the
        images'), with the given disk images (virtio-blk, hard disks to the guest) and SSD images (NVMe), and the
        systemd credentials given, or the autologin one, which the scenarios get their root shell through. The TPM's
        state is <workdir>/<tpm>: the VM's own TPM, one for the whole test, unless another one is named. With
        secure_boot, on OVMF's Secure Boot build, whose variable store lasts from one start to the next (in setup
        mode at first), and QEMU exits when the guest reboots."""
        self.stop()
        url = run_qemu.boot_url(server or self.server, name)
        if secure_boot:
            vars_path = os.path.join(self.workdir, run_qemu.SECBOOT_VARS)
            if os.path.exists(vars_path):
                run_qemu.set_boot_uri(vars_path, url)
            else:
                run_qemu.write_vars(vars_path, url, secure_boot=True)
        else:
            vars_path = os.path.join(self.workdir, "OVMF_VARS.fd")
            run_qemu.write_vars(vars_path, url)
        if tpm_interface:
            self.tpm = run_qemu.Swtpm(os.path.join(self.workdir, tpm))
        if credentials is None:
            credentials = ["agetty.autologin=root"]
        self.probe_port = free_port()
        cmd = run_qemu.qemu_command(vars_path, serial="stdio", forward=(self.probe_port, PROBE_PORT),
                                    credentials=credentials, tpm=self.tpm, tpm_interface=tpm_interface,
                                    secure_boot=secure_boot, disks=disks, ssds=ssds)
        log("starting QEMU: " + " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.console = Console(self.proc, os.path.join(self.workdir, "serial.log"))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        if self.tpm:
            self.tpm.stop()
        self.tpm = None

    def wait_exit(self, timeout):
        """Wait for QEMU to exit by itself, e.g. when the guest reboots on Secure Boot firmware."""
        try:
            self.proc.wait(timeout * MULT)
        except subprocess.TimeoutExpired:
            raise Timeout(f"QEMU did not exit within {timeout * MULT:.0f}s")

    def send(self, text):
        self.proc.stdin.write(text.encode())
        self.proc.stdin.flush()

    def wait_shell(self, timeout=600):
        start = time.monotonic()
        m, _ = self.console.expect(r"login: root \(automatic login\)|login: $", timeout, allow_boot=True)
        if "automatic" not in m[0]:
            self.send("root\n")
        for _ in range(60):
            # The quotes keep the terminal's echo of the input from matching.
            self.send("echo @@REA\"\"DY@@\n")
            try:
                self.console.expect(r"@@READY@@$", 5)
                break
            except Timeout:
                continue
        else:
            raise TestFailure("no shell on the serial console")
        # No echo, no kernel messages, no pagers or colours on the serial console.
        self.send("stty -echo; dmesg -n 1; export SYSTEMD_PAGER=cat PAGER=cat SYSTEMD_COLORS=0\n")
        self.run("true")
        return time.monotonic() - start

    def run(self, cmd, timeout=120, check_rc=True):
        self.n += 1
        n = self.n
        self.send(f"echo @@B\"\"{n}@@; ( {cmd} ) 2>&1; echo @@E\"\"{n}:$?@@\n")
        self.console.expect(rf"@@B{n}@@$", timeout)
        m, output = self.console.expect(rf"@@E{n}:(\d+)@@$", timeout)
        rc = int(m[1])
        output = output.strip("\n")
        if check_rc and rc != 0:
            raise TestFailure(f"`{cmd}` exited with {rc}:\n{output}")
        return output if check_rc else (rc, output)

    def settle(self):
        """Wait for boot to finish; return systemd's system state."""
        rc, state = self.run("systemctl is-system-running --wait", timeout=600, check_rc=False)
        state = state.splitlines()[-1] if state else ""
        if state != "running":
            details = self.run("systemctl --failed --no-legend --plain; "
                               "journalctl -b -p warning --no-pager | tail -n 60", check_rc=False)[1]
            log(f"system state {state!r}:\n{details}")
        return state

    def fstype(self, mountpoint):
        return self.run(f"awk '$5 == \"{mountpoint}\" {{ t = $0 }} END {{ print t }}' /proc/self/mountinfo")

    def probe_answers(self):
        """Whether the probe (Smoke.start_probe) echoes a line from outside the VM, through QEMU's port forward.
        QEMU accepts the connection at once and only then connects to the guest: a dropped SYN shows as silence, a
        refused connection as a reset."""
        try:
            with socket.create_connection(("127.0.0.1", self.probe_port), timeout=5 * MULT) as s:
                s.sendall(b"cherry\n")
                got = b""
                while len(got) < 7:
                    chunk = s.recv(7 - len(got))
                    if not chunk:
                        break
                    got += chunk
                return got == b"cherry\n"
        except OSError:
            return False


class Smoke:
    def __init__(self, args):
        self.args = args
        self.workdir = os.path.abspath(args.state)
        if os.path.exists(self.workdir):
            shutil.rmtree(self.workdir)
        os.makedirs(self.workdir)
        self.images = os.path.abspath(args.images)
        check(os.path.exists(os.path.join(self.images, run_qemu.EFI_NAME)), f"no {run_qemu.EFI_NAME} in {self.images}")
        self.server = run_qemu.serve(self.images)
        self.vm = VM(self.workdir, self.server)
        # The machine's disk: blank, until the agent gives it to the pool cherry (s2), and kept along with the TPM.
        self.var_disk = os.path.join(self.workdir, "var.img")
        run_qemu.make_disk(self.var_disk, 6 << 30)

    def boot(self, tpm_interface="crb", banner=None, secure_boot=False, server=None, tpm="tpm", disks=(), ssds=(),
             expect="running", name=run_qemu.EFI_NAME):
        vm = self.vm
        run_qemu.ImageHandler.requests.clear()
        start = time.monotonic()
        vm.start(tpm_interface, secure_boot=secure_boot, server=server, tpm=tpm, disks=disks, ssds=ssds, name=name)
        if banner:
            vm.console.expect(re.escape(banner), 600, allow_boot=True)
        vm.wait_shell()
        took = time.monotonic() - start
        check(f"/{name}" in run_qemu.ImageHandler.requests,
              f"the firmware did not download the EFI binary over HTTP: {run_qemu.ImageHandler.requests}")
        log(f"HTTP booted to a shell in {took:.0f}s")
        state = vm.settle()
        check(state == expect, f"system is {state!r}, not {expect}")

    def check_networkd_state(self):
        """systemd-networkd still owns /run/systemd/netif and never failed to write its state files there."""
        vm = self.vm
        owners = vm.run("stat -c '%n %U:%G' /run/systemd/netif /run/systemd/netif/links /run/systemd/netif/leases")
        check(all(line.endswith(" systemd-network:systemd-network") for line in owners.splitlines()),
              f"systemd-network no longer owns its runtime directories:\n{owners}")
        rc, errors = vm.run("journalctl -b -u systemd-networkd --no-pager | grep -E 'Failed to update .*state file'",
                            check_rc=False)
        check(rc == 1, "systemd-networkd could not write its state files:\n" + "\n".join(errors.splitlines()[-5:]))

    def start_probe(self):
        """A service of the host's, answering on every address of the host and port PROBE_PORT, as a daemon would:
        systemd-socket-activate accepts each connection and runs cat on it, inetd-style, which echoes a line back.
        What the firewall keeps from answering outside the VM, and the containers."""
        self.vm.run(f"systemd-run -q --unit={PROBE} --collect systemd-socket-activate --accept --inetd "
                    f"-l {PROBE_PORT} /bin/cat && for i in $(seq 50); do "
                    f"grep -qi ':{PROBE_PORT:04X} ' /proc/net/tcp /proc/net/tcp6 && exit 0; sleep 0.1; done; exit 1")

    def stop_probe(self):
        self.vm.run(f"systemctl stop {PROBE}.service")

    def check_firewall(self):
        """The firewall was loaded before systemd-networkd started, lets the router advertisements of QEMU's network
        in (they give the host an IPv6 address), and keeps the probe, a service of the host's, from answering
        outside the VM until a rule lets it in; restarting the firewall takes that rule back out."""
        vm = self.vm
        check(vm.run("systemctl is-active cherry-firewall.service") == "active", "the firewall is not loaded")
        loaded, started = (int(vm.run(f"systemctl show -p {p} --value {unit}")) for p, unit in
                           (("ActiveEnterTimestampMonotonic", "cherry-firewall"),
                            ("InactiveExitTimestampMonotonic", "systemd-networkd")))
        check(loaded <= started, "systemd-networkd started before the firewall was loaded")
        vm.run("for i in $(seq 60); do ip -6 addr show | grep -q 'inet6 fec0:' && exit 0; sleep 1; done; "
               "ip -6 addr show; exit 1", timeout=90)
        self.start_probe()
        check(not vm.probe_answers(), "the probe answers outside the VM, through the firewall")
        vm.run(f"nft add rule inet cherry input tcp dport {PROBE_PORT} accept")
        check(vm.probe_answers(), "the probe does not answer outside the VM once a rule lets it in")
        vm.run("systemctl restart cherry-firewall.service")
        check(not vm.probe_answers(), "restarting the firewall did not take the rule for the probe back out")
        self.stop_probe()

    def check_identity(self, interface):
        """The machine ID comes from the endorsement key of the TPM on the given interface; return it."""
        vm = self.vm
        driver = vm.run("basename $(readlink /sys/class/tpm/tpm0/device/driver)")
        # e.g. tpm_crb_acpi for CRB
        check(driver.startswith(f"tpm_{interface}"), f"the TPM is driven by {driver}, not tpm_{interface}")
        ek_hash = vm.run("cat /run/cherry/identity/ek-hash")
        ek = base64.b64decode(vm.run("base64 /run/cherry/identity/ek.der"))
        check(ek.startswith(P256_SPKI_PREFIX) and len(ek) == len(P256_SPKI_PREFIX) + 64,
              f"the EK is not an uncompressed P-256 public key: {ek.hex()}")
        check(hashlib.sha256(ek).hexdigest() == ek_hash, f"ek-hash {ek_hash!r} is not the SHA-256 of ek.der")
        machine_id = vm.run("cat /etc/machine-id")
        check(machine_id == ek_hash[:32], f"the machine ID {machine_id} is not derived from the EK hash {ek_hash}")
        # The hostname: systemd fills the ?s of the image's /etc/hostname from the machine ID.
        pattern = vm.run("cat /etc/hostname")
        hostname = vm.run("cat /proc/sys/kernel/hostname")
        check("?" in pattern and hostname == hostname_from_machine_id(pattern, machine_id),
              f"the hostname {hostname} is not derived from the machine ID {machine_id} by /etc/hostname's {pattern}")
        # The TPM's own EK: creating it again gives the same key.
        vm.run("tpm2_createek -T device:/dev/tpmrm0 -G ecc -c /tmp/ek.ctx -f der -u /tmp/ek.der >/dev/null && "
               "cmp /tmp/ek.der /run/cherry/identity/ek.der; rc=$?; rm -f /tmp/ek.ctx /tmp/ek.der; exit $rc")
        rc, error = vm.run("cat /run/cherry/identity/error", check_rc=False)
        check(rc != 0, f"an identity error was recorded: {error}")
        log(f"machine identity: EK hash {ek_hash} via {driver}, hostname {hostname}")
        return machine_id

    def check_pcr11(self, uki, phase):
        """PCR 11 holds what the image's policy expects once this boot phase is done: the stub's measurement of the UKI
        (its sections, as read from the file), extended by systemd's phases."""
        expected = pcrsign.measure(uki, phase).hex()
        pcr = self.vm.run("tpm2_pcrread sha256:11 | sed -n 's/.*0x//p'").lower()
        check(pcr == expected, f"PCR 11 is {pcr}, not {expected}, which the image's policy expects after {phase!r}")

    def check_sealed(self, partition, uki):
        """The LUKS2 volume's one TPM token seals its key to PCR 7, and to PCR 11 under the image's PCR key."""
        tokens = json.loads(self.vm.run(f"cryptsetup luksDump --dump-json-metadata /dev/{partition}"))["tokens"]
        tpm2 = [t for t in tokens.values() if t.get("type") == "systemd-tpm2"]
        check(len(tpm2) == 1 and tpm2[0].get("tpm2-pcrs") == [7] and tpm2[0].get("tpm2_pubkey_pcrs") == [11]
              and base64.b64decode(tpm2[0].get("tpm2_pubkey", "")) == bytes(uki[".pcrpkey"]),
              f"/dev/{partition}'s key is not sealed to PCR 7 and PCR 11 under the image's key: {tpm2}")

    def dm_of(self, name):
        """The kernel name (dm-N) of the device-mapper device called name."""
        return self.vm.run(f"grep -lx {name} /sys/class/block/dm-*/dm/name | cut -d / -f 5")

    def check_var_mount(self):
        """/var is the btrfs of /dev/mapper/cherry-var, the pool cherry's volume var, without nosuid: its dm-N."""
        vm = self.vm
        var = vm.fstype("/var")
        check(" - btrfs " in var, f"/var is not a btrfs: {var}")
        check("nosuid" not in var.split(" - ")[0], f"/var is nosuid, which breaks setuid in containers: {var}")
        # The mount's device number is btrfs's own (an anonymous one per subvolume), not the block device's: the
        # source names the device, which is the volume's mapping.
        source = var.split(" - ")[1].split()[1]
        check(source == "/dev/mapper/cherry-var", f"/var is not on /dev/mapper/cherry-var: {var}")
        dm = self.dm_of("cherry-var")
        # That node is the device itself, not a link to /dev/dm-N: compare the numbers (stat prints them in hex).
        major, minor = vm.run("stat -L -c %t:%T /dev/mapper/cherry-var").split(":")
        check(f"{int(major, 16)}:{int(minor, 16)}" == vm.run(f"cat /sys/class/block/{dm}/dev"),
              f"/dev/mapper/cherry-var is not {dm}")
        check(vm.run(f"cat /sys/class/block/{dm}/dm/uuid").startswith("LVM-"), "cherry-var is not a logical volume")
        return dm

    def check_var_in_ram(self):
        """/var is cherry/var on a zram device of 132 MiB, zstd, the pool cherry's one physical volume, tagged ram:
        the pool in RAM, var 128 MiB, as cherry-storage.service made it."""
        vm = self.vm
        dm = self.check_var_mount()
        zram = vm.run(f"ls /sys/class/block/{dm}/slaves")
        check(re.fullmatch(r"zram[1-9][0-9]*", zram), f"cherry/var is not on a zram device alone: {zram}")
        check(vm.run(f"cat /sys/block/{zram}/disksize") == str(132 << 20)
              and "[zstd]" in vm.run(f"cat /sys/block/{zram}/comp_algorithm"), f"{zram} is not 132 MiB, zstd")
        check(vm.run("lvs --noheadings --units m --nosuffix -o lv_size cherry/var").strip() == "128.00",
              "cherry/var is not 128 MiB")
        check(vm.run("pvs --noheadings -o pv_name,pv_tags").split() == [f"/dev/{zram}", "ram"],
              "LVM sees other physical volumes than the pool cherry's zram device, tagged ram")
        vm.run(f"journalctl -b -u cherry-storage --no-pager -o cat | grep -q 'in RAM on /dev/{zram}'")

    def check_var_on_pool(self, partition):
        """/var is cherry/var on the LUKS2 volume of the given partition, labelled cherry-pv, which the TPM unlocks:
        the pool cherry on its disk, uncached; no zram device but the swap's is left."""
        vm = self.vm
        dm = self.check_var_mount()
        pv = vm.run(f"ls /sys/class/block/{dm}/slaves")
        check(vm.run(f"cat /sys/class/block/{pv}/dm/name") == f"cherry-pv-{partition}"
              and vm.run(f"cat /sys/class/block/{pv}/dm/uuid").startswith("CRYPT-LUKS2-"),
              f"cherry/var is not on the LUKS2 volume cherry-pv-{partition}: {pv}")
        check(vm.run(f"ls /sys/class/block/{pv}/slaves") == partition, f"cherry-pv-{partition} is not on {partition}")
        vm.run(f"grep -qx PARTNAME=cherry-pv /sys/class/block/{partition}/uevent")
        check(vm.run("ls /dev/zram*") == "/dev/zram0", "a zram device besides the swap's is left")

    def check_lockdown(self):
        """The kernel is locked down in integrity mode, and loads only modules the build signed: every module the
        image carries is, one of them loads (a driver for hardware the VM lacks fails with ENODEV, so the first
        that does), and a copy of it without its signature (the PKCS#7 signature, the struct module_signature and
        the magic string the build appended) is refused."""
        vm = self.vm
        lockdown = vm.run("cat /sys/kernel/security/lockdown")
        check(lockdown == "none [integrity] confidentiality",
              f"the kernel is not locked down in integrity mode: {lockdown}")
        check(vm.run("cat /sys/module/module/parameters/sig_enforce") == "Y", "module signatures are not enforced")
        modules = vm.run("find /lib/modules -name '*.ko' | sort").split()
        check(modules, "the image carries no kernel module")
        for ko in modules:
            signer = vm.run(f"modinfo -F signer {ko}")
            check(signer == "Build time autogenerated kernel key", f"{ko} is not signed by the build's key: {signer!r}")
        for ko in modules:
            name = os.path.basename(ko)[:-3].replace("-", "_")
            rc, out = vm.run(f"modprobe {name}", check_rc=False)
            if rc == 0:
                check(vm.run(f"grep -c '^{name} ' /proc/modules") == "1", f"{name} loaded, but is not in /proc/modules")
                vm.run(f"modprobe -r {name}")
                break
            log(f"{name} does not load on this VM: {out.strip()}")
        else:
            check(False, f"none of the image's {len(modules)} modules loads on this VM")
        vm.run(f"size=$(stat -c %s {ko}); [ \"$(tail -c 28 {ko} | tr -d '\\n')\" = '~Module signature appended~' ]; "
               f"set -- $(od -An -tu1 -j$((size - 40)) -N12 {ko}); "
               "head -c $((size - 40 - $4 - $5 - ($9 << 24) - (${10} << 16) - (${11} << 8) - ${12})) "
               f"{ko} >/tmp/unsigned.ko; [ -z \"$(modinfo -F signer /tmp/unsigned.ko)\" ]")
        rc, out = vm.run("insmod /tmp/unsigned.ko", check_rc=False)
        check(rc != 0 and "Key was rejected by service" in out, f"the unsigned module was not refused: {rc} {out}")
        check(vm.run(f"grep -c '^{name} ' /proc/modules || true") == "0", f"{name} is loaded after the refusal")
        vm.run("rm /tmp/unsigned.ko")
        log(f"lockdown: {lockdown}; {len(modules)} module(s) signed by the build's key; {ko} loaded, and its unsigned "
            "copy refused")

    def s1_http_boot(self):
        vm = self.vm
        self.boot(disks=[self.var_disk])

        root = vm.fstype("/")
        check(re.match(r"\S+ \S+ \S+ / / ro[ ,]", root), f"/ is not mounted read-only: {root}")
        check(" - erofs /rootfs.erofs " in root, f"/ is not the EROFS image from the initramfs: {root}")
        rc, _ = vm.run("touch /cherry-rw-test", check_rc=False)
        check(rc != 0, "the root filesystem is writable")
        self.check_var_in_ram()
        # Nothing claims a disk at boot: the blank disk is as it was.
        check(vm.run("wipefs --no-act --noheadings /dev/vda; ls -d /sys/class/block/vda/vda* 2>/dev/null; true") == "",
              "the blank disk was written to at boot")
        check(vm.run("readlink -f /root") == "/var/roothome", "/root is not on /var")
        check("NAME=Cherry" in vm.run("cat /etc/os-release"), "unexpected os-release")
        check("console=ttyS0" in vm.run("cat /proc/cmdline"), "the UKI command line was not used")
        self.machine_id = self.check_identity("crb")
        failed = vm.run("systemctl --failed --no-legend --plain")
        check(failed == "", f"failed units:\n{failed}")
        self.check_lockdown()
        vm.run("for i in $(seq 60); do ip -4 addr show | grep -q 'inet 10\\.0\\.2\\.' && exit 0; sleep 1; done; "
               "networkctl; exit 1", timeout=90)
        self.check_firewall()
        # systemd arms it before journald runs, so its message is only in the kernel log.
        vm.run("test -c /dev/watchdog0 && dmesg | grep -q 'Using hardware watchdog'")
        check(vm.run("systemctl show -p RuntimeWatchdogUSec --value") == "30s", "RuntimeWatchdogSec not applied")
        log("memory of the booted OS, before any container:\n" + vm.run(
            "free -k; df -k /var /run | sed 's/^/  /'; "
            "grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|Shmem|Slab|KernelStack|PageTables):' "
            "/proc/meminfo"))

    def s2_persist(self):
        vm = self.vm
        url = "http://127.0.0.1:3000"
        # Through the storage MCP server, as the agent would (s14 checks the configuration that gives it the server):
        # the pool cherry in RAM alone, var its volume, and the blank disk free.
        state = json.loads(self.storage("list", {})[0])
        found = {d["name"]: d for d in state["disks"]}
        pools = {p["name"]: p for p in state["pools"]}
        check(state["var"]["ram"] is True and list(pools) == ["cherry"] and pools["cherry"].get("ram")
              and pools["cherry"]["disks"] == [] and pools["cherry"]["volumes"][0]["path"] == "/var",
              f"the pool cherry is not in RAM alone, with var: {state}")
        check((found["vda"]["state"], found["vda"]["blank"]) == ("free", True),
              f"vda is not a free, blank disk: {found['vda']}")
        # What is there before the move: cherry.service's server, online, a file of the cherry user's in its home,
        # and one of root's.
        vm.run(f"for i in $(seq 120); do wget -q -O - {url}/health && exit 0; sleep 1; done; "
               "journalctl -b -u cherry --no-pager | tail -n 20; exit 1", timeout=240)
        pid = vm.run("systemctl show -p MainPID --value cherry.service")
        vm.run(f"{AS_CHERRY} {IN_CHERRY_SERVICE} sh -c 'echo before >/var/lib/cherry/work/smoke-persist'")
        vm.run("echo cherry-smoke >/var/cherry-smoke")
        # The disk claimed: a LUKS2 volume with the TPM's key alone, holding a physical volume.
        text = self.storage("claim", {"disks": [{"name": "vda", "fingerprint": found["vda"]["fingerprint"]}]},
                            timeout=600)[0]
        check("claimed /dev/vda: /dev/vda1, unlocked with the TPM as /dev/mapper/cherry-pv-vda1" in text,
              f"vda not claimed:\n{text}")
        luks = vm.run("cryptsetup luksDump /dev/vda1")
        check(re.search(r"^\s+\d+: systemd-tpm2$", luks, re.M) and len(re.findall(r"^\s+\d+: luks2$", luks, re.M)) == 1,
              f"/dev/vda1 is not a LUKS2 volume with the TPM's key alone:\n{luks}")
        # Its key sealed to the boot state: PCR 7, and PCR 11 under the image's PCR key, whose signed policy checks
        # out and holds what PCR 11 is now that the machine is ready; the key and the signature are where systemd
        # looks for them.
        image = os.path.join(self.images, run_qemu.EFI_NAME)
        p = subprocess.run([sys.executable, pcrsign.__file__, "check", image], capture_output=True, text=True)
        check(p.returncode == 0, f"the image's PCR policy does not check out: {p.stdout}{p.stderr}")
        log(p.stdout.strip())
        _, uki = pcrsign.read_uki(image)
        self.check_sealed("vda1", uki)
        self.check_pcr11(uki, "sysinit:ready")
        vm.run("test -s /run/systemd/tpm2-pcr-signature.json && test -s /run/systemd/tpm2-pcr-public-key.pem")
        check("sealing" not in text, f"a new volume's key was sealed anew:\n{text}")
        # Given to the pool cherry: /var moves onto it, live.
        text, pool = self.storage("create_pool", {"name": "cherry", "disks": ["vda"]}, timeout=900)
        log("create_pool cherry:\n" + text)
        check("moving /var from RAM onto the pool's disks" in text and "/var is on the pool's disks now" in text,
              f"/var did not move:\n{text}")
        check(pool["disks"] == ["vda"] and not pool.get("ram") and pool["volumes"][0]["name"] == "var"
              and pool["volumes"][0]["path"] == "/var" and pool["volumes"][0]["size"] == "128.0 MiB",
              f"the pool is not as made: {pool}")
        self.check_var_on_pool("vda1")
        # Nothing stopped or restarted, and the files are there.
        check(vm.run("systemctl show -p MainPID --value cherry.service") == pid, "cherry.service restarted")
        health = json.loads(vm.run(f"wget -q -O - {url}/health"))
        check(health["status"] == "ok", f"OpenChamber is not well after the move: {health}")
        check(vm.run("cat /var/cherry-smoke /var/lib/cherry/work/smoke-persist") == "cherry-smoke\nbefore",
              "files written before the move are gone")
        check(vm.run("pvs --noheadings -o pv_name").split() == ["/dev/mapper/cherry-pv-vda1"],
              "LVM sees other physical volumes than the pool cherry's disk")
        # var grows to 4 GiB, and /var with it.
        pool = self.storage("resize_volume", {"pool": "cherry", "name": "var", "size": "4G"})[1]
        size = int(vm.run("df -B 1 --output=size /var | tail -n 1"))
        check(pool["volumes"][0]["size"] == "4.0 GiB" and size > 3.5 * (1 << 30), f"/var did not grow: {size}, {pool}")
        state = json.loads(self.storage("list", {})[0])
        found = {d["name"]: d for d in state["disks"]}
        check(state["var"]["ram"] is False
              and (found["vda"]["state"], found["vda"]["pool"], found["vda"]["role"]) == ("claimed", "cherry", "data"),
              f"after the move: {state}")

    def s3_nspawn(self):
        vm = self.vm
        root = "/var/lib/machines/smoke"
        vm.run(f"rm -rf {root} && mkdir -p {root}/usr {root}/etc && "
               f"for d in proc sys dev run tmp; do mkdir {root}/$d; done && "
               f"cp /usr/lib/os-release {root}/etc/os-release && "
               f"for l in bin sbin lib lib64; do [ -L /$l ] && ln -s $(readlink /$l) {root}/$l; done; ls -l {root}")
        # pacstrap's pacman hooks run `systemd-tmpfiles --create` chrooted in the new root, which
        # looks up systemd-network in the new root's passwd. arch-chroot sets up the same chroot.
        vm.run(f"sed -E 's/^(systemd-network:x:)[0-9]+:[0-9]+:/\\1192:192:/' /etc/passwd >{root}/etc/passwd && "
               f"sed -E 's/^(systemd-network:x:)[0-9]+:/\\1192:/' /etc/group >{root}/etc/group && "
               f"mount --bind /usr {root}/usr && "
               f"{{ arch-chroot {root} systemd-tmpfiles --create /usr/lib/tmpfiles.d/systemd-network.conf; rc=$?; "
               f"umount {root}/usr; exit $rc; }}")
        self.check_networkd_state()
        vm.run(f"systemd-run --unit=smoke-nspawn systemd-nspawn -M smoke -D {root} --bind-ro=/usr "
               "--network-veth /bin/sh -c 'ip link set host0 up && exec sleep 100000'")
        vm.run("for i in $(seq 60); do machinectl show smoke -p State --value 2>/dev/null | grep -qx running "
               "&& exit 0; sleep 1; done; journalctl -u smoke-nspawn --no-pager; exit 1", timeout=120)
        vm.run("for i in $(seq 60); do networkctl status ve-smoke | grep -Eq 'State: .*\\(configured' "
               "&& exit 0; sleep 1; done; networkctl status ve-smoke; exit 1", timeout=120)
        vm.run("nft list table ip io.systemd.nat")
        self.check_networkd_state()
        # Through the firewall, the container leases an address from networkd's DHCP server, pings the host, and
        # reaches out of the VM, masqueraded, to the HTTP server that served the image; but the probe, a service
        # of the host's, answers it neither on its link, over IPv4 or IPv6, nor on the host's own address, though
        # it answers the host itself. Commands run with the host's tools in the container's network namespace.
        inside = ("systemd-run -q --wait --pipe --collect "
                  "-p NetworkNamespacePath=/proc/$(machinectl show smoke -p Leader --value)/ns/net")

        def probe_answers(address, prefix=""):
            rc, out = vm.run(f"{prefix} timeout {5 * MULT:g} bash -c 'exec 3<>/dev/tcp/{address}/{PROBE_PORT} "
                             "&& echo cherry >&3 && head -c 7 <&3'", timeout=60, check_rc=False)
            return rc == 0 and out == "cherry"

        out = vm.run(f"{inside} udhcpc -i host0 -f -q -n -t 10 -T 2 -s /bin/true", timeout=120)
        lease = re.search(r"lease of (\S+) obtained", out)
        check(lease, f"the container got no lease from networkd's DHCP server:\n{out}")
        gateway, prefix = vm.run("ip -4 -o addr show dev ve-smoke scope global | awk '{ print $4 }'").split("/")
        vm.run(f"{inside} sh -c 'ip addr add {lease[1]}/{prefix} dev host0 && ip route add default via {gateway}'")
        vm.run(f"{inside} ping -c 1 -W 5 {gateway}", timeout=60)
        vm.run(f"{inside} wget -q -T 10 -t 1 --spider {run_qemu.boot_url(self.server)}", timeout=60)
        address = vm.run("ip -4 -o addr show | sed -n 's/.* inet \\(10\\.0\\.2\\.[0-9]*\\)\\/.*/\\1/p'")
        local = vm.run("ip -6 -o addr show dev ve-smoke scope link | sed -n 's/.* inet6 \\(fe80::[^/]*\\)\\/.*/\\1/p'")
        check(local, "the container's link has no IPv6 link-local address on the host")
        self.start_probe()
        for target in (address, "::1"):
            check(probe_answers(target), f"the probe does not answer the host itself on {target}")
        for target in (gateway, address, f"{local}%host0"):
            check(not probe_answers(target, inside),
                  f"the probe answers the container on {target}, through the firewall")
        self.stop_probe()
        vm.run("machinectl terminate smoke; for i in $(seq 30); do machinectl show smoke >/dev/null 2>&1 || exit 0; "
               "sleep 1; done; exit 1", timeout=60)

    def s4_persistent_reboot(self):
        vm = self.vm
        # Powered off, not pulled: the volumes, /var and the pool go down in order.
        vm.send("systemctl poweroff\n")
        vm.wait_exit(300)
        # The same TPM, attached through the other interface: the disk unlocked, var mounted, the files there.
        self.boot(tpm_interface="tis", disks=[self.var_disk])
        journal = "journalctl -b -u cherry-storage --no-pager -o cat"
        shown = vm.run(journal)
        log("cherry-storage.service:\n" + shown)
        check("/var is cherry/var (btrfs) on the pool's disks, unlocked with the TPM" in shown and "RAM" not in shown
              and "sealing" not in shown, f"cherry-storage.service did not mount the pool's var alone:\n{shown}")
        # Left clean, it has no tree log for the mount to replay.
        check("tree-log replay" not in vm.run("dmesg | grep BTRFS || true"),
              "/var was not unmounted cleanly at the poweroff: the mount replayed a tree log")
        self.check_var_on_pool("vda1")
        check(vm.run("cat /var/cherry-smoke /var/lib/cherry/work/smoke-persist") == "cherry-smoke\nbefore",
              "files on /var did not survive the reboot")
        check(vm.run("systemctl is-active cherry-storage.service") == "active", "cherry-storage.service is not active")
        check(self.check_identity("tis") == self.machine_id, "the machine ID changed across a reboot")

        # On btrfs, machinectl clone is a snapshot: an environment shares its image's blocks until it changes them. The
        # trees cherry-bootstrap and cherry-image make there are subvolumes for it (tests/bootstrap.sh); this one is by
        # hand.
        vm.run("btrfs subvolume create /var/lib/machines/cow-image >/dev/null && "
               "echo shared >/var/lib/machines/cow-image/file && machinectl clone cow-image cow-env")
        image_uuid = vm.run("btrfs subvolume show /var/lib/machines/cow-image | awk '$1 == \"UUID:\" { print $2 }'")
        clone = vm.run("btrfs subvolume show /var/lib/machines/cow-env")
        check(re.search(rf"^\s*Parent UUID:\s+{image_uuid}\s*$", clone, re.M),
              f"cow-env is not a snapshot of cow-image ({image_uuid}):\n{clone}")
        check(vm.run("cat /var/lib/machines/cow-env/file") == "shared", "the snapshot lacks the image's file")
        types = vm.run("machinectl list-images --no-legend | awk '$1 ~ /^cow-/ { print $1, $2 }' | sort")
        check(types == "cow-env subvolume\ncow-image subvolume", f"not two subvolumes: {types}")
        vm.run("machinectl remove cow-env cow-image && test ! -e /var/lib/machines/cow-env")

        # Another machine's TPM cannot unlock the disk: the pool is not seen, so /var is in RAM, the host key is
        # another, nothing claims the disk, and it is left as it was.
        vm.send("systemctl poweroff\n")
        vm.wait_exit(300)
        before = file_sha256(self.var_disk)
        self.boot(tpm="tpm-other", disks=[self.var_disk])
        vm.run(f"{journal} | grep -q 'the TPM cannot unlock /dev/vda1'")
        self.check_var_in_ram()
        found = {d["name"]: d for d in json.loads(self.storage("list", {})[0])["disks"]}
        check(found["vda"]["state"] == "locked", f"the disk is not locked on another machine: {found['vda']}")
        vm.send("systemctl poweroff\n")
        vm.wait_exit(300)
        check(file_sha256(self.var_disk) == before, "the disk was written to on another machine")
        # Back on its own TPM, everything is there.
        self.boot(disks=[self.var_disk])
        self.check_var_on_pool("vda1")
        check(vm.run("cat /var/cherry-smoke") == "cherry-smoke", "a file on /var did not survive the other machine")

    def s5_debootstrap(self):
        vm = self.vm
        mirror = "http://deb.debian.org/debian"
        # systemd-resolved can fail lookups for a while after boot (DNSSEC, DoT probing), so retry.
        rc, out = vm.run(f"for i in $(seq 12); do wget -q -T 10 -t 1 -O /dev/null {mirror}/dists/trixie/InRelease "
                         "&& exit 0; sleep 5; done; resolvectl query deb.debian.org; exit 1",
                         timeout=240, check_rc=False)
        if rc != 0:
            log(f"skipped: the guest cannot reach {mirror}: {out}")
            return SKIPPED
        root = "/var/lib/machines/debian"
        # The whole lifecycle as the cherry user: cherry-bootstrap@.service runs debootstrap as root for it (its
        # log readable), and polkit lets it drive machinectl.
        log_file = "/var/log/cherry-bootstrap/debian:trixie:debian.log"
        vm.run(f"{AS_CHERRY} systemctl start cherry-bootstrap@debian:trixie:debian "
               f"|| {{ tail -n 40 {log_file}; exit 1; }}", timeout=1800)
        check(vm.run(f"stat -c %a {log_file}") == "644", "the bootstrap log is not readable by the cherry user")
        check("VERSION_CODENAME=trixie" in vm.run(f"cat {root}/etc/os-release"), "debootstrap did not install trixie")
        vm.run(f"{AS_CHERRY} machinectl start debian")
        # The container's console goes to the host journal. Wait there rather than with
        # `systemctl -M debian is-system-running --wait`, which can block forever when started this early.
        vm.run("for i in $(seq 120); do journalctl -u systemd-nspawn@debian --no-pager "
               "| grep -Eq 'Reached target (multi-user\\.target|Multi-User System)' && exit 0; sleep 1; done; "
               "journalctl -u systemd-nspawn@debian --no-pager | tail -n 40; exit 1", timeout=300)
        rc, state = vm.run("systemctl -M debian is-system-running", check_rc=False)
        check(state in ("running", "degraded"), f"the Debian container is {state!r}")
        if state != "running":
            log("the Debian container is degraded:\n" + vm.run("systemctl -M debian --failed --no-legend --plain"))
        version = vm.run("systemd-run -M debian --wait -q -P cat /etc/debian_version")
        check(version.startswith("13"), f"unexpected /etc/debian_version in the container: {version!r}")
        vm.run(f"{AS_CHERRY} machinectl terminate debian; "
               "for i in $(seq 30); do machinectl show debian >/dev/null 2>&1 || exit 0; sleep 1; done; exit 1",
               timeout=60)
        vm.run(f"{AS_CHERRY} machinectl remove debian")
        rc, _ = vm.run(f"test -e {root}", check_rc=False)
        check(rc != 0, "machinectl remove left the container tree behind")

    def s6_rpmstrap(self):
        vm = self.vm
        host = "mirrors.fedoraproject.org"
        # systemd-resolved can fail lookups for a while after boot (DNSSEC, DoT probing), so retry.
        # rpmstrap only uses HTTPS mirrors, so probe with a verified HTTPS request (there is no
        # timeout(1) on the image). It fails, and the scenario skips, when the mirror is unreachable
        # or when TLS is intercepted by a CA the image does not trust.
        url = f"https://{host}/metalink?repo=fedora-44&arch=x86_64"
        rc, out = vm.run(f"for i in $(seq 12); do wget -q -T 10 -t 1 -O /dev/null '{url}' && exit 0; sleep 5; done; "
                         f"wget -T 10 -t 1 -O /dev/null '{url}' 2>&1 | tail -n 3; exit 1", timeout=300, check_rc=False)
        if rc != 0:
            log(f"skipped: the guest cannot reach {host}: {out}")
            return SKIPPED
        root = "/var/lib/machines/fedora"
        vm.run(f"rpmstrap fedora 44 {root} >/tmp/rpmstrap.log 2>&1 || {{ tail -n 40 /tmp/rpmstrap.log; exit 1; }}",
               timeout=1800)
        check("VERSION_ID=44" in vm.run(f"cat {root}/etc/os-release"), "rpmstrap did not install Fedora 44")
        vm.run("machinectl start fedora")
        # As for Debian: wait for the container's boot in the host journal.
        vm.run("for i in $(seq 120); do journalctl -u systemd-nspawn@fedora --no-pager "
               "| grep -Eq 'Reached target (multi-user\\.target|Multi-User System)' && exit 0; sleep 1; done; "
               "journalctl -u systemd-nspawn@fedora --no-pager | tail -n 40; exit 1", timeout=300)
        rc, state = vm.run("systemctl -M fedora is-system-running", check_rc=False)
        check(state in ("running", "degraded"), f"the Fedora container is {state!r}")
        if state != "running":
            log("the Fedora container is degraded:\n" + vm.run("systemctl -M fedora --failed --no-legend --plain"))
        # The container's own rpm finds the database that the host's rpm wrote.
        vm.run("systemd-run -M fedora --wait -q -P rpm -q fedora-release systemd dnf5")
        vm.run("machinectl terminate fedora; for i in $(seq 30); do machinectl show fedora >/dev/null 2>&1 || exit 0; "
               "sleep 1; done; exit 1", timeout=60)
        vm.run(f"rm -rf {root}")

    def s7_pacstrap(self):
        vm = self.vm
        root = "/var/lib/machines/arch"
        # The keyring is built offline at every boot, without holding up boot.
        vm.run("systemctl start pacman-init.service", timeout=300)
        keys = vm.run("pacman-key --list-keys | grep -c '^pub'")
        check(int(keys) > 20, f"the pacman keyring holds only {keys} keys")
        # pacman itself probes the mirror, so a proxy set for pacstrap applies here too.
        rc, out = vm.run("d=$(mktemp -d) && timeout 60 pacman --dbpath $d -Sy; rc=$?; rm -rf $d; exit $rc",
                         timeout=120, check_rc=False)
        if rc != 0:
            log(f"skipped: the Arch Linux mirrors are unreachable:\n{out}")
            return SKIPPED
        vm.run(f"mkdir -p {root} && pacstrap -K -c {root} base", timeout=3600)
        log("pacstrap: " + vm.run(f"du -sh {root} /var/cache/pacman/pkg | tr '\\n' ' '"))
        vm.run("rm -rf /var/cache/pacman/pkg/*")
        caps = vm.run(f"chroot {root} getcap /usr/bin/newuidmap")
        check("cap_setuid" in caps, f"newuidmap lost its file capability: {caps!r}")
        vm.run(f"chroot {root} pacman-key --list-keys >/dev/null")
        # The container's own pacman downloads as DownloadUser=alpm in a Landlock sandbox. --pipe:
        # no pty, so no OSC 3008 context sequences on the console.
        vm.run(f"systemd-nspawn -q --pipe -D {root} ${{https_proxy:+--setenv=https_proxy=$https_proxy}} "
               "pacman -Syy --noconfirm | cat", timeout=600)
        vm.run("machinectl start arch")
        # As in s5_debootstrap: `systemctl -M` fails until the container's bus is up, so wait for
        # its console output in the host journal first. Arch's systemd names targets by their
        # description only ("Reached target Multi-User System.").
        vm.run("for i in $(seq 300); do journalctl -u systemd-nspawn@arch --no-pager "
               "| grep -q -E 'Reached target (multi-user\\.target|Multi-User System)' && exit 0; sleep 1; done; "
               "journalctl -u systemd-nspawn@arch --no-pager | tail -n 40; exit 1", timeout=600)
        rc, state = vm.run("systemctl -M arch is-system-running --wait", timeout=300, check_rc=False)
        state = state.splitlines()[-1] if state else ""
        if state != "running":
            log(f"container state {state!r}:\n" + vm.run("systemctl -M arch --failed --no-legend --plain",
                                                         check_rc=False)[1])
        check(state in ("running", "degraded"), f"the Arch container is {state!r}")
        check("ID=arch" in vm.run("systemd-run -M arch --wait -q -P cat /etc/os-release"),
              "the container is not Arch Linux")
        self.check_networkd_state()
        vm.run("machinectl terminate arch; for i in $(seq 30); do machinectl show arch >/dev/null 2>&1 || exit 0; "
               "sleep 1; done; exit 1", timeout=60)
        vm.run(f"rm -rf {root}")

    def s8_opencode(self):
        vm = self.vm
        version = package_version("opencode")
        # Stripped by Buildroot, the Bun single-file executable would run as a bare Bun and print Bun's version.
        check(vm.run("opencode --version") == f"opencode v{version}",
              f"opencode --version is not the packaged {version}")
        # Its grep tool is the image's ripgrep, and git is on the image for its snapshots and worktrees.
        vm.run("test -x /usr/bin/rg && test -x /usr/bin/git")
        # Only the binary: the opencode user, /var/lib/opencode and opencode.service come with
        # BR2_PACKAGE_OPENCODE_SERVICE, off in Cherry. OpenChamber runs the server, as the cherry user (s9).
        rc, _ = vm.run("getent passwd opencode", check_rc=False)
        check(rc != 0, "there is an opencode user")
        rc, _ = vm.run("test -e /var/lib/opencode || test -e /usr/lib/systemd/system/opencode.service", check_rc=False)
        check(rc != 0, "/var/lib/opencode or opencode.service is on the image")

    def s9_cherry(self):
        vm = self.vm
        version = package_version("openchamber")
        check(vm.run("systemctl is-active cherry.service") == "active", "cherry.service is not running")
        pid = vm.run("systemctl show -p MainPID --value cherry.service")
        check(vm.run(f"stat -c %U /proc/{pid}") == "cherry", "the OpenChamber server does not run as the cherry user")
        check(vm.run(f"readlink /proc/{pid}/exe") == "/usr/bin/bun", "the OpenChamber server does not run on Bun")
        check(vm.run(f"readlink /proc/{pid}/cwd") == "/var/lib/cherry/work",
              "the OpenChamber server does not work in /var/lib/cherry/work")
        # Its system call filter applies (systemd built with seccomp): mode 2, a filter.
        check(vm.run(f"awk '/^Seccomp:/ {{ print $2 }}' /proc/{pid}/status") == "2",
              "the OpenChamber server runs without cherry.service's system call filter")
        url = "http://127.0.0.1:3000"
        health = vm.run(f"for i in $(seq 120); do wget -q -O - {url}/health && exit 0; sleep 1; done; "
                        "journalctl -b -u cherry --no-pager | tail -n 20; exit 1", timeout=240)
        health = json.loads(health)
        check((health["status"], health["openchamberVersion"]) == ("ok", version), f"unexpected health: {health}")
        # The OpenCode it runs is the packaged one; it takes a few seconds more to come up.
        rc, compat = vm.run(f"for i in $(seq 120); do c=$(wget -q -O - {url}/api/opencode/compatibility); "
                            "case \"$c\" in *'\"compatible\"'*) echo \"$c\"; exit 0;; esac; sleep 1; done; "
                            "echo \"$c\"; exit 1", timeout=240, check_rc=False)
        check(rc == 0, f"OpenChamber's OpenCode is not up: {compat}")
        compat = json.loads(compat)
        check(compat["version"] == package_version("opencode"),
              f"OpenChamber does not run the packaged OpenCode: {compat}")
        # Once the server was online, cherry-connect-url.service showed the link that pairs another OpenChamber app
        # with it, and its QR code, on the consoles (before the login prompts) and in the journal:
        # `openchamber connect-url --relay --qr`, run as the cherry user.
        check(vm.run("systemctl is-active cherry-connect-url.service") == "active",
              "cherry-connect-url.service did not succeed")
        journal = "journalctl -b -u cherry-connect-url --no-pager -o cat"
        shown = vm.run(journal)
        check("openchamber://connect?v=2&p=" in shown, f"no pairing link in cherry-connect-url's journal:\n{shown}")
        # The QR code's modules are block characters (U+2588 is a full one).
        rc, _ = vm.run(f"{journal} | grep -q -F \"$(printf '\\342\\226\\210')\"", check_rc=False)
        check(rc == 0, "no QR code in cherry-connect-url's journal")
        check(vm.run("stat -c %U /var/lib/cherry/.config/openchamber/client-pairing-sessions.json") == "cherry",
              "the pairing session was not created as the cherry user")
        # Its container privileges, through polkit: machinectl's image operations are allowed (so the failure is
        # the missing image, not a denial), starting cherry-bootstrap@ is allowed (the script rejects an empty
        # machine name), and any other unit is denied.
        vm.run(f"{AS_CHERRY} machinectl list --no-legend")
        rc, out = vm.run(f"{AS_CHERRY} machinectl remove cherry-smoke-none", check_rc=False)
        check(rc != 0 and "denied" not in out.lower() and "authentication" not in out.lower(),
              f"machinectl remove as cherry did not fail on the missing image alone: {out}")
        rc, out = vm.run(f"{AS_CHERRY} systemctl start cherry-bootstrap@debian:trixie:", check_rc=False)
        check(rc != 0 and "denied" not in out.lower() and "authentication" not in out.lower(),
              f"starting cherry-bootstrap@ as cherry did not fail on the empty machine name alone: {out}")
        vm.run("systemctl reset-failed 'cherry-bootstrap@debian:trixie:.service'")
        # `machinectl start` is a start of systemd-nspawn@<name>.service, allowed too. machinectl first asks
        # machined whether the image exists, so the probe gives the name one: an empty directory, which nspawn
        # refuses as not an OS tree. The start then reaches systemd and fails in nspawn, not on authorization,
        # leaving the unit failed (machinectl 258 reports the failed job by its exit status alone).
        vm.run("mkdir /var/lib/machines/cherry-smoke-empty")
        rc, out = vm.run(f"{AS_CHERRY} machinectl start cherry-smoke-empty", check_rc=False)
        check(rc != 0 and "denied" not in out.lower() and "authentication" not in out.lower(),
              f"machinectl start as cherry did not fail on the empty tree alone: {out}")
        rc, state = vm.run("systemctl is-failed systemd-nspawn@cherry-smoke-empty.service", check_rc=False)
        check(rc == 0, f"machinectl start as cherry did not start systemd-nspawn@cherry-smoke-empty.service "
                       f"(state {state!r}): {out}")
        vm.run("systemctl reset-failed systemd-nspawn@cherry-smoke-empty.service && "
               "rmdir /var/lib/machines/cherry-smoke-empty")
        rc, out = vm.run(f"{AS_CHERRY} systemctl start cherry-connect-url.service", check_rc=False)
        check(rc != 0 and ("denied" in out.lower() or "authentication" in out.lower()),
              f"the cherry user may start units other than cherry-bootstrap@: {out}")
        # Everything in the service, OpenCode included, runs as the cherry user.
        users = vm.run("for p in $(cat /sys/fs/cgroup/system.slice/cherry.service/cgroup.procs); do "
                       "stat -c %U /proc/$p 2>/dev/null; done | sort -u")
        check(users == "cherry", f"cherry.service has processes of other users: {users}")
        check("OpenChamber" in vm.run(f"wget -q -O - {url}/"), "the web UI is not served")
        # Everything in the service listens on loopback only: OpenChamber on 3000 and its OpenCode on a free port.
        # The listening TCP sockets of the service's processes, matched by inode in /proc/net/tcp and tcp6.
        listening = vm.run("inodes=$(for p in $(cat /sys/fs/cgroup/system.slice/cherry.service/cgroup.procs); do "
                           "ls -l /proc/$p/fd 2>/dev/null; done | sed -n 's/.*socket:\\[\\([0-9]*\\)\\].*/\\1/p'); "
                           "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk -v inodes=\"$inodes\" "
                           "'BEGIN { n = split(inodes, a, \"\\n\"); for (i = 1; i <= n; i++) want[a[i]] } "
                           "$4 == \"0A\" && ($10 in want) { print $2 }'")
        sockets = sorted({tcp_local_address(a) for a in listening.split()})
        check(len(sockets) >= 2 and 3000 in [port for _, port in sockets]
              and all(ipaddress.ip_address(ip).is_loopback for ip, _ in sockets),
              f"cherry.service does not listen on loopback only, on 3000 and OpenCode's port: {sockets}")
        log("cherry.service listens on " + ", ".join(f"{ip}:{port}" for ip, port in sockets))
        address = vm.run("ip -4 addr show scope global | sed -n 's/.* inet \\([0-9.]*\\)\\/.*/\\1/p' | head -n 1")
        rc, _ = vm.run(f"wget -q -T 5 -t 1 -O /dev/null http://{address}:3000/health", check_rc=False)
        check(rc != 0, f"OpenChamber answers on {address}, not only on loopback")
        owners = vm.run("stat -c '%n %U:%G' /var/lib/cherry /var/lib/cherry/work /var/lib/cherry/.config/openchamber")
        check(all(line.endswith(" cherry:cherry") for line in owners.splitlines()),
              f"/var/lib/cherry does not belong to the cherry user:\n{owners}")
        # The openchamber package's own service is off too (BR2_PACKAGE_OPENCHAMBER_SERVICE): no openchamber user,
        # /var/lib/openchamber or openchamber.service.
        rc, _ = vm.run("getent passwd openchamber", check_rc=False)
        check(rc != 0, "there is an openchamber user")
        rc, _ = vm.run("test -e /var/lib/openchamber || test -e /usr/lib/systemd/system/openchamber.service",
                       check_rc=False)
        check(rc != 0, "/var/lib/openchamber or openchamber.service is on the image")
        rss_kb = int(vm.run(f"awk '/^VmRSS:/ {{ print $2 }}' /proc/{pid}/status"))
        log(f"OpenChamber {version}: {rss_kb >> 10} MiB resident")

    def mcp(self, tool, arguments, timeout=120, server="machines", error=False):
        """Call a tool of one of OpenCode's MCP servers in /etc/opencode, machines or storage, as OpenCode in
        cherry.service would: the reply's text, which must not be an error, or must be one with error."""
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "cherry-smoke", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": tool, "arguments": arguments}}]
        requests = base64.b64encode("".join(json.dumps(m) + "\n" for m in messages).encode()).decode()
        # The console's shell takes lines of up to 1024 characters: the requests go in pieces.
        for i in range(0, len(requests), 600):
            self.vm.run(f"printf %s {requests[i:i + 600]} {'>' if i == 0 else '>>'}/tmp/mcp-requests.b64")
        out = self.vm.run(f"base64 -d /tmp/mcp-requests.b64 >/tmp/mcp-requests && {AS_CHERRY} {IN_CHERRY_SERVICE} "
                          f"/usr/bin/bun /usr/lib/cherry/{server}-mcp.js </tmp/mcp-requests", timeout=timeout)
        for line in out.splitlines():
            if line.startswith("{") and json.loads(line).get("id") == 2:
                result = json.loads(line)["result"]
                text = result["content"][0]["text"]
                if error:
                    check(result["isError"], f"the MCP tool {tool} did not fail:\n{text}")
                else:
                    check(not result["isError"], f"the MCP tool {tool} failed:\n{text}")
                return text
        raise TestFailure(f"no reply from the MCP tool {tool}:\n{out}")

    def s10_images(self):
        vm = self.vm
        mirror = "http://deb.debian.org/debian"
        # As in s5_debootstrap.
        rc, out = vm.run(f"for i in $(seq 12); do wget -q -T 10 -t 1 -O /dev/null {mirror}/dists/trixie/InRelease "
                         "&& exit 0; sleep 5; done; resolvectl query deb.debian.org; exit 1",
                         timeout=240, check_rc=False)
        if rc != 0:
            log(f"skipped: the guest cannot reach {mirror}: {out}")
            return SKIPPED
        # OpenCode's system configuration names the MCP server and denies the agent every other tool; the skill, the
        # verbs and the shipped profiles come with it. (A home of its own, apart from the OpenCode that OpenChamber
        # runs.)
        config = vm.run(f"{AS_CHERRY} -p PrivateTmp=yes -E HOME=/tmp opencode debug config")
        check('"path": "/etc/opencode/opencode.json"' in config and "machines-mcp.js" in config,
              f"OpenCode does not read /etc/opencode/opencode.json:\n{config}")
        # OpenCode reads the permission block into rules, printed in the file's order, and the last rule that matches
        # wins: the deny of everything first, then what the agent may use.
        documents = json.loads(config[config.index("["):])
        system = next(d for d in documents if d.get("path") == "/etc/opencode/opencode.json")
        rules = [(r["action"], r["resource"], r["effect"]) for r in system["info"]["permissions"]]
        check(rules == [("*", "*", "deny"), ("machines_*", "*", "allow"), ("storage_*", "*", "allow"),
                        ("skill", "*", "allow"), ("question", "*", "allow")],
              f"OpenCode's permissions do not deny the agent's own tools:\n{rules}")
        vm.run("test -s /etc/opencode/skills/machine-images.md && test -s /usr/lib/cherry/environments.js")
        vm.run("test -s /usr/lib/cherry/profiles/debian-trixie/image.conf && "
               "test -x /usr/lib/cherry/profiles/debian-trixie/postinst")
        ids = [p["id"] for p in json.loads(self.mcp("list", {}))["profiles"]]
        check({"debian-trixie", "ubuntu-noble", "fedora-44", "arch"} <= set(ids),
              f"the shipped profiles are not listed: {ids}")

        # A profile of the machine's own, written as the cherry user: the smoke test's recipe.
        image, env = "smoke-base", "smoke-env"
        root, clone = f"/var/lib/machines/{image}", f"/var/lib/machines/{env}"
        motd = "built by cherry-image for the smoke test"
        profile = "/var/lib/cherry/profiles/smoke"
        files = {
            "image.conf": ("0644", "Distribution=debian\nRelease=trixie\nPackages=iproute2\n"),
            "profile.conf": ("0644", "Title=Smoke test\nDescription=The smoke test's own profile\n"),
            "extra/etc/motd": ("0644", motd + "\n"),
            "extra/usr/bin/smoke-hello": ("0755", "#!/bin/sh\necho hello from $(cat /proc/sys/kernel/hostname)\n"),
            "postinst": ("0755", "#!/bin/sh\nset -eu\nsystemctl enable systemd-networkd\n"
                                 "echo postinst ran >/var/postinst-ran\n"),
            "image.nspawn": ("0644", "[Exec]\nEnvironment=SMOKE=1\n"),
        }
        vm.run(f"{AS_CHERRY} rm -rf {profile}")
        for rel, (mode, content) in files.items():
            data = base64.b64encode(content.encode()).decode()
            vm.run(f"echo {data} | base64 -d >/tmp/smoke-file && "
                   f"{AS_CHERRY} install -D -m {mode} /tmp/smoke-file {profile}/{rel}")
        check("smoke" in [p["id"] for p in json.loads(self.mcp("list", {}))["profiles"]],
              "the machine's own profile is not listed")

        # The image: built from the profile, never started.
        text = self.mcp("build", {"name": image, "profile": "smoke"}, timeout=1800)
        log("build:\n" + text)
        check(text.startswith(f"Built the image {image} from the profile smoke.") and f"built {root} " in text,
              f"the build's log does not end as it should:\n{text}")
        check(vm.run(f"stat -c %a /var/log/cherry-image/build:{image}.log") == "644",
              "the build log is not readable by the cherry user")
        check(vm.run(f"stat -c %U /var/lib/cherry/images/{image}/image.conf") == "cherry",
              "the recipe was not written as the cherry user")
        check(vm.run(f"cat /var/lib/cherry/images/{image}/profile.conf").endswith("Profile=smoke"),
              "the recipe does not name its profile")
        check(vm.run(f"cat {root}.nspawn") == "[Exec]\nEnvironment=SMOKE=1", "the image's settings were not installed")
        vm.run(f"test -e {root}/etc/machine-id && ! test -s {root}/etc/machine-id && "
               f"test -z \"$(ls {root}/var/lib/apt/lists)\" && ! test -e {root}/.cherry-image")
        check(vm.run(f"cat {root}/etc/motd {root}/var/postinst-ran") == f"{motd}\npostinst ran",
              "the profile's files and postinst did not land in the image")
        log(f"{image}: " + vm.run(f"du -sh {root} | cut -f 1"))
        text = self.mcp("start", {"name": image}, error=True)
        check("is an image" in text, f"starting the image was not refused as such:\n{text}")

        # The environment: a clone of the image, with its settings, recorded as the cherry user.
        text = self.mcp("create", {"name": env, "image": image}, timeout=600)
        check(text == f"Created the environment {env} from the image {image}.", f"create answered:\n{text}")
        vm.run(f"test -d {clone} && test -f {clone}/usr/bin/smoke-hello")
        check(vm.run(f"cat {clone}.nspawn") == "[Exec]\nEnvironment=SMOKE=1",
              "the clone did not get the image's settings")
        record = f"/var/lib/cherry/environments/{env}/environment.conf"
        check(vm.run(f"stat -c %U {record}") == "cherry" and vm.run(f"cat {record}").startswith(f"Image={image}\n"),
              f"the environment's record is not the cherry user's, naming its image: {vm.run(f'cat {record}')}")
        listed = json.loads(self.mcp("list", {}))
        check([i["name"] for i in listed["images"]] == [image] and listed["images"][0]["environments"] == [env] and
              [(e["name"], e["image"]) for e in listed["environments"]] == [(env, image)],
              f"list does not tell the image from its environment:\n{listed}")
        text = self.mcp("remove_image", {"name": image}, error=True)
        check("still has environments" in text, f"removing the image under its environment was not refused:\n{text}")

        check(self.mcp("start", {"name": env}, timeout=300) == f"{env} is running", f"{env} did not start")
        # The agent has no tool to run a command in it; root on the host has (stdin kept open: the pty forwarder
        # ends at EOF). Its login messages may come first.
        command = "cat /etc/motd /var/postinst-ran; smoke-hello"
        text = vm.run(f"sleep 10 | machinectl -q shell {env} /bin/sh -c '{command}' | tr -d '\\r'", timeout=60)
        check(f"{motd}\npostinst ran\nhello from {env}" in text,
              f"the environment does not answer as it should:\n{text}")
        # Its network: host0, configured by the image's networkd with DHCP from the host, as status reads it.
        status = None
        for _ in range(20):
            status = json.loads(self.mcp("status", {"name": env}, timeout=180))
            if status.get("address"):
                break
            vm.run("sleep 3")
        check(status["kind"] == "environment" and status["image"] == image and status["running"] and status["exists"],
              f"status does not describe the environment:\n{status}")
        check(re.match(r"\d+\.\d+\.\d+\.\d+/\d+$", status.get("address") or ""), f"host0 has no address:\n{status}")
        check(status["system"] in ("running", "degraded", "starting") and
              "systemd-networkd.service" not in status["failed"], f"systemd in {env} is not up:\n{status}")
        log(f"{env}: systemd {status['system']}, host0 {status['address']}, failed units {status['failed']}")
        status = json.loads(self.mcp("status", {"name": image}))
        check(status["kind"] == "image" and status["profile"] == {"id": "smoke", "title": "Smoke test"} and
              not status["running"], f"status does not describe the image:\n{status}")

        # OpenChamber's Environments extension: a built-in, listed with its grants and nothing to click, whose
        # service, started by the first request, answers as the cherry user in cherry.service's sandbox.
        url = "http://127.0.0.1:3000"
        guest_id = "openchamber-builtin-cherry-environments"
        guests = json.loads(vm.run(f"wget -q -O - {url}/api/guests"))["guests"]
        guest = next((g for g in guests if g["id"] == guest_id), None)
        check(guest is not None, f"OpenChamber does not list the Environments extension:\n{guests}")
        check(guest["source"] == "bundled" and guest["enabled"] and guest["service"]["granted"] and
              "service" in guest["capabilities"]["granted"] and any(c["name"] == "env" for c in guest["commands"]),
              f"the Environments extension is not a granted built-in with the /env command:\n{guest}")
        body = json.dumps({"method": "GET", "path": "/environments"})
        reply = json.loads(vm.run(f"wget -q -O - --header 'Content-Type: application/json' --post-data '{body}' "
                                  f"{url}/api/guests/{guest_id}/service/request", timeout=120))
        check(reply.get("status") == 200, f"the Environments service did not answer:\n{reply}")
        listed = json.loads(reply["body"])
        check([i["name"] for i in listed["images"]] == [image] and
              [(e["name"], e["image"], e["running"]) for e in listed["environments"]] == [(env, image, True)] and
              "smoke" in [p["id"] for p in listed["profiles"]],
              f"the service lists otherwise than the MCP server:\n{listed}")
        # (A loop's status is its last iteration's: the last process is not the service.)
        pids = vm.run("for p in /proc/[0-9]*; do grep -qF cherry-environments/service/main.js $p/cmdline 2>/dev/null "
                      "&& echo ${p#/proc/}; done; true")
        check(pids, "the Environments service is not running")
        for pid in pids.split():
            check(vm.run(f"stat -c %U /proc/{pid}") == "cherry" and
                  vm.run(f"readlink /proc/{pid}/exe") == "/usr/bin/bun" and
                  vm.run(f"awk '/^Seccomp:/ {{ print $2 }}' /proc/{pid}/status") == "2",
                  f"the Environments service (pid {pid}) does not run as cherry, on Bun, under the system call filter")

        check(self.mcp("stop", {"name": env}, timeout=180) == f"{env} is stopped", f"{env} did not stop")

        # Enabled, it starts at boot: Enabled=yes in its record, which cherry-autostart.service reads once /var is
        # mounted and starts systemd-nspawn@ for, before machines.target, as machinectl enable would have systemd do
        # were /etc not read-only. The image is not enabled, and nothing is started until the boot.
        text = self.mcp("enable", {"name": image}, error=True)
        check("is an image" in text, f"enabling the image was not refused as such:\n{text}")
        check(self.mcp("enable", {"name": env}) == f"{env} is enabled: it starts at boot", f"{env} was not enabled")
        check(vm.run(f"stat -c %U {record}") == "cherry" and vm.run(f"grep -c '^Enabled=' {record}") == "1" and
              vm.run(f"grep -x Enabled=yes {record}") == "Enabled=yes",
              f"the record is not enabled: {vm.run(f'cat {record}')}")
        check(vm.run(f"systemctl is-active systemd-nspawn@{env}.service", check_rc=False)[1] == "inactive",
              "enable started the environment")
        listed = json.loads(self.mcp("list", {}))
        check([(e["name"], e["enabled"], e["running"]) for e in listed["environments"]] == [(env, True, False)],
              f"list does not show the environment enabled:\n{listed}")
        check(vm.run("systemctl is-enabled cherry-autostart.service") == "enabled" and
              vm.run("systemctl show -p WantedBy --value cherry-autostart.service") == "machines.target",
              "cherry-autostart.service is not wanted by machines.target")
        vm.send("systemctl poweroff\n")
        vm.wait_exit(300)
        self.boot(disks=[self.var_disk])
        self.check_var_on_pool("vda1")
        check(vm.run(f"systemctl is-active systemd-nspawn@{env}.service") == "active", f"{env} did not start at boot")
        check(vm.run("systemctl show -p Result --value cherry-autostart.service") == "success",
              "cherry-autostart.service did not succeed")
        shown = vm.run("journalctl -b -u cherry-autostart --no-pager -o cat")
        check(f"starting: systemd-nspawn@{env}.service" in shown,
              f"cherry-autostart.service did not start {env}:\n{shown}")
        up, reached = (int(vm.run(f"systemctl show -p ActiveEnterTimestampMonotonic --value {unit}")) for unit in
                       (f"systemd-nspawn@{env}.service", "machines.target"))
        check(0 < up <= reached, f"machines.target was reached ({reached}) before {env} was up ({up})")
        status = json.loads(self.mcp("status", {"name": env}, timeout=180))
        check(status["running"] and status["enabled"] and status["image"] == image,
              f"status does not show the environment running and enabled after the boot:\n{status}")
        text = vm.run(f"sleep 10 | machinectl -q shell {env} /bin/sh -c '{command}' | tr -d '\\r'", timeout=60)
        check(f"hello from {env}" in text, f"the environment started at boot does not answer:\n{text}")

        # Disabled, with now: stopped, and the unit run again, as the next boot would, starts nothing.
        text = self.mcp("disable", {"name": env, "now": True}, timeout=180)
        check(text == f"{env} is disabled: it does not start at boot. {env} is stopped", f"disable answered:\n{text}")
        check(vm.run(f"grep -x Enabled=no {record}") == "Enabled=no",
              f"the record is not disabled: {vm.run(f'cat {record}')}")
        check(vm.run(f"systemctl is-active systemd-nspawn@{env}.service", check_rc=False)[1] == "inactive",
              f"{env} is still running after disable with now")
        vm.run("systemctl restart cherry-autostart.service")
        shown = vm.run("journalctl -b -u cherry-autostart --no-pager -o cat")
        check(shown.count("starting: ") == 1 and "no environment is enabled" in shown,
              f"cherry-autostart.service, run again, did not find nothing to start:\n{shown}")
        check(vm.run(f"systemctl is-active systemd-nspawn@{env}.service", check_rc=False)[1] == "inactive",
              f"{env} was started by the unit after disable")
        check(json.loads(self.mcp("status", {"name": env}))["enabled"] is False, "status still shows it enabled")

        # Images never leave the machine: cherry-image@ takes no export verb, and writes nothing out.
        rc, out = vm.run(f"{AS_CHERRY} systemctl start cherry-image@tar:{image}.service", check_rc=False)
        check(rc != 0 and "denied" not in out.lower() and "authentication" not in out.lower(),
              f"cherry-image@tar:{image} as cherry did not fail on its instance alone: {out}")
        rc, _ = vm.run("test -e /var/lib/cherry/exports", check_rc=False)
        check(rc != 0, "cherry-image@ wrote an export")

        # The environment goes with its record, then the image.
        check(self.mcp("remove", {"name": env}) == f"{env} removed", f"{env} was not removed")
        rc, _ = vm.run(f"test -e {clone} || test -e {clone}.nspawn || test -e {record}", check_rc=False)
        check(rc != 0, "the environment, its settings or its record are still there")
        check(self.mcp("remove_image", {"name": image}) == f"{image} removed", f"{image} was not removed")
        rc, _ = vm.run(f"test -e {root} || test -e {root}.nspawn", check_rc=False)
        check(rc != 0, "the image or its settings are still there")
        vm.run(f"rm -rf /var/lib/cherry/images/{image} {profile}")

    def s11_zram(self):
        vm = self.vm
        mem_kb = int(vm.run("awk '/^MemTotal:/ { print $2 }' /proc/meminfo"))
        swaps = vm.run("tail -n +2 /proc/swaps")
        fields = swaps.split()
        # Size in kB: all of zram0 but the page that holds the swap header.
        check(len(fields) == 5 and fields[0] == "/dev/zram0" and abs(int(fields[2]) - mem_kb) <= 8,
              f"zram0 is not the only swap device, as large as the RAM ({mem_kb} kB):\n{swaps}")
        check("[zstd]" in vm.run("cat /sys/block/zram0/comp_algorithm"), "zram does not compress with zstd")
        check(vm.run("cat /proc/sys/vm/page-cluster") == "0", "vm.page-cluster is not 0")
        # Write 400 MiB of text to a tmpfs (/dev/shm: /var is the pool's btrfs) from a cgroup limited to 128 MiB:
        # its own shmem pages have to go to zram, compressed.
        size = 400 << 20
        data = f"yes cherry-zram | head -c {size}"
        vm.run(f"systemd-run --wait -q -p MemoryMax=128M sh -c '{data} >/dev/shm/zram-test'", timeout=300)
        stats = vm.run("cat /sys/block/zram0/mm_stat").split()
        stored, compressed = int(stats[0]), int(stats[1])
        check(stored >= 200 << 20, f"only {stored} bytes went to zram under memory pressure: {stats}")
        log(f"zram holds {stored >> 20} MiB in {compressed >> 20} MiB")
        check(vm.run("md5sum </dev/shm/zram-test") == vm.run(f"{data} | md5sum"),
              "data that went through zram came back changed")
        vm.run("rm /dev/shm/zram-test")

    def s12_no_tpm(self):
        vm = self.vm
        self.boot(tpm_interface=None, banner=NO_TPM_BANNER)
        error = vm.run("cat /run/cherry/identity/error")
        check(error == "no TPM 2.0 found", f"unexpected identity error: {error}")
        rc, _ = vm.run("test -e /run/cherry/identity/ek-hash", check_rc=False)
        check(rc != 0, "an EK hash without a TPM")
        check(vm.run("systemctl is-active cherry-no-tpm.target") == "active", "cherry-no-tpm.target is not active")
        rc, states = vm.run("systemctl is-active multi-user.target systemd-networkd.service cherry.service "
                            "machines.target", check_rc=False)
        check(set(states.split()) == {"inactive"}, f"units started without a TPM:\n{states}")
        failed = vm.run("systemctl --failed --no-legend --plain")
        check(failed == "", f"failed units:\n{failed}")
        vm.run("journalctl -b -u cherry-no-tpm.service --no-pager | grep -q 'Stopped: no usable TPM 2.0'")

    def efi_variables(self):
        """{name: data} of the Secure Boot firmware's variable store, read on the host."""
        path = os.path.join(self.workdir, "variables.json")
        subprocess.run([run_qemu.virt_fw_vars(), "--input", os.path.join(self.workdir, run_qemu.SECBOOT_VARS),
                        "--output-json", path], check=True, stdout=subprocess.DEVNULL)
        with open(path) as f:
            return {v["name"]: bytes.fromhex(v["data"]) for v in json.load(f)["variables"]}

    def s13_secure_boot(self):
        vm = self.vm
        keys = "/usr/share/cherry/secureboot"
        rc, _ = vm.run(f"test -s {keys}/PK.auth", check_rc=False)
        if rc != 0:
            log("skipped: the image has no Secure Boot keys (scripts/secureboot.py init writes them)")
            return SKIPPED
        for tool in ("openssl", "sbsign", "sbverify"):
            check(shutil.which(tool), f"{tool} not found: apt install openssl sbsigntool")
        image = os.path.join(self.images, run_qemu.EFI_NAME)
        # A build with a local chain signs its image (board/x86_64/post-image.sh): it boots under the keys it
        # enrolls as it is, where an unsigned one needs the test's CA in db.
        signed = "signature 1" in subprocess.run(["sbverify", "--list", image], capture_output=True, text=True).stdout
        sets = {var: secureboot.parse_auth2(base64.b64decode(vm.run(f"base64 {keys}/{var}.auth")))[2]
                for var in ("PK", "KEK", "db")}

        # Setup mode: the keys go in, and the guest reboots, which ends QEMU.
        vm.start(secure_boot=True)
        # The banner, folded: any space may be a line break and its indentation.
        enrolled = re.compile(r"Cherry\s+enrolled\s+its\s+Secure\s+Boot\s+keys:\s+rebooting\s+into\s+Secure\s+Boot\."
                              r"[\s\S]*?\s+CA\s+and\s+(\d+)\s+option\s+ROM")
        m, _ = vm.console.expect(enrolled, 900, allow_boot=True)
        drivers = int(m[1])
        vm.wait_exit(120)
        variables = self.efi_variables()
        for var in ("PK", "KEK"):
            check(variables.get(var) == sets[var], f"{var} is not the image's")
        db = secureboot.parse_esl(variables.get("db", b""))
        hashes = [entry for entry in db[1:] if entry[:2] == (secureboot.EFI_CERT_SHA256, secureboot.CHERRY_OWNER)]
        check(db[:1] == secureboot.parse_esl(sets["db"]) and len(hashes) == len(db) - 1 == drivers >= 1,
              f"db is not the image's CA and the {drivers} option ROM driver(s): {db}")
        check("CherrySecureBootEnrolled" in variables, "the enrollment is not marked")
        log(f"enrolled PK, KEK, and db: the CA and {drivers} option ROM driver(s)")

        if signed:
            log("the build signed the image with a chain of its own: it boots as it is")
            self.boot(secure_boot=True)
        else:
            # Secure Boot is on: the image, unsigned, is refused.
            vm.start(secure_boot=True)
            vm.console.expect(r"Access Denied|Security Violation", 600, allow_boot=True)
            vm.stop()
            self.boot_signed_by_test_ca()
        flags = vm.run("cd /sys/firmware/efi/efivars && for v in SecureBoot SetupMode; do "
                       "printf '%s=%s ' $v $(od -An -tu1 -j4 -N1 $v-8be4df61-93ca-11d2-aa0d-00e098032b8c); done")
        check(flags.split() == ["SecureBoot=1", "SetupMode=0"], f"not booted with Secure Boot on: {flags}")
        report = vm.run("journalctl -b -u cherry-secureboot --no-pager -o cat")
        check("Secure Boot is on, with Cherry's keys." in report, f"cherry-secureboot reported:\n{report}")
        rc, _ = vm.run("ls /sys/firmware/efi/efivars/CherrySecureBootEnrolled-*", check_rc=False)
        check(rc != 0, "the enrollment mark was not cleared")
        check(vm.run("systemctl --failed --no-legend --plain") == "", "units failed with Secure Boot on")

    def boot_signed_by_test_ca(self):
        """Boot the image signed by a CA of the test's, added to db, with Secure Boot on."""
        tmp = os.path.join(self.workdir, "secureboot")
        os.makedirs(os.path.join(tmp, "images"))
        for cmd in (["req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{tmp}/ca.key",
                     "-subj", "/CN=Cherry smoke test CA/", "-days", "1", "-out", f"{tmp}/ca.crt"],
                    ["req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{tmp}/signer.key",
                     "-CA", f"{tmp}/ca.crt", "-CAkey", f"{tmp}/ca.key", "-subj", "/CN=Cherry smoke test signer/",
                     "-days", "1", "-addext", "basicConstraints=critical,CA:false", "-out", f"{tmp}/signer.crt"]):
            subprocess.run(["openssl", *cmd], check=True, capture_output=True)
        subprocess.run([run_qemu.virt_fw_vars(), "--inplace", os.path.join(self.workdir, run_qemu.SECBOOT_VARS),
                        "--add-db", str(secureboot.CHERRY_OWNER), f"{tmp}/ca.crt"], check=True, capture_output=True)
        subprocess.run(["sbsign", "--key", f"{tmp}/signer.key", "--cert", f"{tmp}/signer.crt", "--output",
                        os.path.join(tmp, "images", run_qemu.EFI_NAME), os.path.join(self.images, run_qemu.EFI_NAME)],
                       check=True, capture_output=True)
        server = run_qemu.serve(os.path.join(tmp, "images"))
        try:
            self.boot(secure_boot=True, server=server)
        finally:
            server.shutdown()

    def storage(self, tool, arguments, timeout=300, error=False):
        """A tool of the storage MCP server; for those that change a pool, the pool as it is after, too."""
        text = self.mcp(tool, arguments, timeout=timeout, server="storage", error=error)
        at = text.rfind("\n\n{")
        return text, (json.loads(text[at + 2:]) if at >= 0 and not error else None)

    def s14_storage(self):
        vm = self.vm
        # The machine's disk, with the pool cherry on it; two hard disks, one blank and one with a partition table; an
        # SSD with a partition table too.
        images = {name: os.path.join(self.workdir, f"storage-{name}.img") for name in ("hdd0", "hdd1", "ssd0")}
        for name, size in (("hdd0", 8 << 30), ("hdd1", 8 << 30), ("ssd0", 2 << 30)):
            run_qemu.make_disk(images[name], size)
        for name in ("hdd1", "ssd0"):
            with open(images[name], "r+b") as f:
                f.write(DOS_MBR)
        disks, ssds = [self.var_disk, images["hdd0"], images["hdd1"]], [images["ssd0"]]
        self.boot(disks=disks, ssds=ssds)
        self.check_var_on_pool("vda1")
        config = vm.run(f"{AS_CHERRY} -p PrivateTmp=yes -E HOME=/tmp opencode debug config")
        check("storage-mcp.js" in config, f"OpenCode's configuration has no storage MCP server:\n{config}")
        vm.run("for s in storage configure-storage manage-storage; do test -s /etc/opencode/skills/$s.md || exit; done")

        state = json.loads(self.storage("list", {})[0])
        found = {d["name"]: d for d in state["disks"]}
        log("disks: " + ", ".join(f"{d['name']} {d['kind']} {d['size']} {d['state']}" for d in state["disks"]))
        check((found["vda"]["state"], found["vda"]["pool"], found["vda"]["role"]) == ("claimed", "cherry", "data")
              and state["var"] == {"fstype": "btrfs", "source": "/dev/mapper/cherry-var", "ram": False},
              f"vda is not the pool cherry's, with /var on it: {state}")
        check((found["vdb"]["state"], found["vdb"]["kind"], found["vdb"]["blank"]) == ("free", "hdd", True),
              f"vdb is not a free, blank hard disk: {found['vdb']}")
        check((found["vdc"]["state"], found["vdc"]["blank"]) == ("free", False) and "dos" in found["vdc"]["signatures"],
              f"vdc is not a free disk with a partition table: {found['vdc']}")
        nvme = found["nvme0n1"]
        check((nvme["state"], nvme["kind"], nvme["blank"]) == ("free", "ssd", False),
              f"nvme0n1 is not a free SSD with a partition table: {nvme}")
        check([p["name"] for p in state["pools"]] == ["cherry"], f"pools before any but cherry: {state['pools']}")

        # A disk is claimed only as it was listed.
        text = self.storage("claim", {"disks": [{"name": "vdb", "fingerprint": "000000000000"}]}, error=True)[0]
        check("is not as it was listed" in text, f"claim with a wrong fingerprint:\n{text}")
        rc, _ = vm.run("test -e /sys/class/block/vdb/vdb1", check_rc=False)
        check(rc != 0, "a refused claim wrote to vdb")
        text = self.storage("claim", {"disks": [{"name": n, "fingerprint": found[n]["fingerprint"]}
                                                for n in ("vdb", "vdc", "nvme0n1")]}, timeout=600)[0]
        for disk, part in (("vdb", "vdb1"), ("vdc", "vdc1"), ("nvme0n1", "nvme0n1p1")):
            check(f"claimed /dev/{disk}: /dev/{part}, unlocked with the TPM" in text, f"{disk} not claimed:\n{text}")
            vm.run(f"grep -qx PARTNAME=cherry-pv /sys/class/block/{part}/uevent")
            luks = vm.run(f"cryptsetup luksDump /dev/{part}")
            check(re.search(r"^\s+\d+: systemd-tpm2$", luks, re.M)
                  and len(re.findall(r"^\s+\d+: luks2$", luks, re.M)) == 1,
                  f"/dev/{part} is not a LUKS2 volume with the TPM's key alone:\n{luks}")
        # LVM sees Cherry's physical volumes only: the pool cherry's and the claimed disks', nothing else.
        pvs = vm.run("pvs --noheadings -o pv_name 2>/dev/null | sort").split()
        check(pvs == ["/dev/mapper/cherry-pv-nvme0n1p1", "/dev/mapper/cherry-pv-vda1", "/dev/mapper/cherry-pv-vdb1",
                      "/dev/mapper/cherry-pv-vdc1"], f"LVM's physical volumes: {pvs}")

        # The pool cherry is a pool like any: cached on the SSD, var with it, under /var's files; and the machine's
        # own: never destroyed, var never removed, its disk never taken out.
        pool = self.storage("set_cache", {"pool": "cherry", "disks": ["nvme0n1"]})[1]
        check(pool["cache"]["disks"] == ["nvme0n1"] and pool["volumes"][0]["cache"]
              and vm.run("lvs --noheadings -o segtype cherry/var").strip() == "cache",
              f"cherry/var is not cached: {pool}")
        check(vm.run("cat /var/cherry-smoke") == "cherry-smoke", "/var lost a file under the cache")
        refused = [("destroy_pool", {"name": "cherry"}, "the pool cherry holds /var: it is never destroyed"),
                   ("remove_volume", {"pool": "cherry", "name": "var"}, "cherry/var is /var: it is never removed"),
                   ("remove_disk", {"pool": "cherry", "disk": "vda"},
                    "the last data disk of the pool cherry, which holds /var: it stays")]
        for tool, args, message in refused:
            text = self.storage(tool, args, error=True)[0]
            check(message in text, f"{tool} on the pool cherry was not refused as it should:\n{text}")
        pool = self.storage("set_cache", {"pool": "cherry", "disks": []})[1]
        check(pool["cache"] is None and vm.run("lvs --noheadings -o segtype cherry/var").strip() == "linear",
              f"cherry/var is still cached: {pool}")

        # A pool of the hard disks, cached on the SSD; a volume, cached, mounted, the cherry user's.
        pool = self.storage("create_pool", {"name": "tank", "disks": ["vdb", "vdc"], "cache": ["nvme0n1"]})[1]
        check(pool["disks"] == ["vdb", "vdc"] and pool["cache"]["disks"] == ["nvme0n1"]
              and pool["cache"]["mode"] == "writethrough", f"the pool is not as made: {pool}")
        pool = self.storage("create_volume", {"pool": "tank", "name": "data", "size": "3G"})[1]
        mp = "/var/lib/cherry/volumes/tank/data"
        volume = pool["volumes"][0]
        check(volume["name"] == "data" and volume["mounted"] and volume["path"] == mp and volume["cache"]
              and volume["cache"]["mode"] == "writethrough", f"the volume is not as made: {pool}")
        log(f"pool tank: {pool['size']}, cache {pool['cache']['size']}; volume data: {volume['size']}, "
            f"cache {volume['cache']['size']}")

        def check_volume():
            mount = vm.fstype(mp)
            options = set(mount.split()[5].split(","))
            # The mount's device number is btrfs's own, not the volume's: the source names the volume.
            check(" - btrfs /dev/mapper/tank-data " in mount and {"nodev", "nosuid"} <= options,
                  f"{mp} is not tank/data's btrfs, nodev and nosuid: {mount}")
            check(vm.run("lvs --noheadings -o segtype tank/data").strip() == "cache", "tank/data is not cached")
            check(vm.run(f"stat -c %U {mp}") == "cherry", f"{mp} is not the cherry user's")

        check_volume()
        vm.run(f"{AS_CHERRY} {IN_CHERRY_SERVICE} sh -c 'echo cherry-storage >{mp}/hello'")
        # cherry.service, which started before the volume was made, sees it, writable.
        pid = vm.run("systemctl show -p MainPID --value cherry.service")
        line = vm.run(f"awk -v mp={mp} '$5 == mp' /proc/{pid}/mountinfo")
        check(line and line.split()[5].split(",")[0] == "rw", f"cherry.service does not see {mp} writable: {line!r}")

        # A data disk out: its data moves to the other one, and the cache comes back.
        pool = self.storage("remove_disk", {"pool": "tank", "disk": "vdb"}, timeout=900)[1]
        check(pool["disks"] == ["vdc"] and pool["volumes"][0]["cache"], f"vdb did not leave the pool: {pool}")
        check(vm.run(f"cat {mp}/hello") == "cherry-storage", "the volume's data did not survive the move")
        pool = self.storage("resize_volume", {"pool": "tank", "name": "data", "size": "5G"})[1]
        size = int(vm.run(f"df -B 1 --output=size {mp} | tail -n 1"))
        check(size > 4.5 * (1 << 30) and pool["volumes"][0]["cache"],
              f"the volume did not grow, or lost its cache: {size}, {pool}")
        rc, out = vm.run(f"{AS_CHERRY} systemctl stop cherry-storage.service", check_rc=False)
        check(rc != 0 and ("denied" in out.lower() or "authentication" in out.lower()),
              f"the cherry user may stop cherry-storage.service: {out}")

        # Powered off, which unmounts and closes it all, and booted again: unlocked, activated, mounted, clean.
        vm.send("systemctl poweroff\n")
        vm.wait_exit(300)
        self.boot(disks=disks, ssds=ssds)
        check(vm.run("systemctl is-active cherry-storage.service") == "active", "cherry-storage.service is not active")
        shown = vm.run("journalctl -b -u cherry-storage --no-pager -o cat")
        check(not re.search(r"cannot|not active|missing disks", shown), f"the storage was not set up:\n{shown}")
        check("tree-log replay" not in vm.run("dmesg | grep BTRFS || true"),
              "a volume was not unmounted cleanly at the poweroff: the mount replayed a tree log")
        check_volume()
        check(vm.run(f"cat {mp}/hello") == "cherry-storage", "the volume's data did not survive the reboot")
        self.check_var_on_pool("vda1")
        check(vm.run("cat /var/cherry-smoke") == "cherry-smoke", "/var's file did not survive the reboot")

        # Taken apart: the cache, the volume, the pool; then the disks, given back blank.
        pool = self.storage("set_cache", {"pool": "tank", "disks": []})[1]
        check(pool["cache"] is None and pool["volumes"][0]["cache"] is None, f"the cache is still there: {pool}")
        self.storage("remove_volume", {"pool": "tank", "name": "data"})
        rc, _ = vm.run(f"test -e {mp}", check_rc=False)
        check(rc != 0, f"{mp} is still there")
        self.storage("destroy_pool", {"name": "tank"})
        state = json.loads(self.storage("list", {})[0])
        found = {d["name"]: d for d in state["disks"]}
        check([p["name"] for p in state["pools"]] == ["cherry"]
              and all((found[n]["state"], found[n]["pool"]) == ("claimed", None) for n in ("vdb", "vdc", "nvme0n1")),
              f"not claimed, in no pool: {state}")
        self.storage("release", {"disks": [{"name": n, "fingerprint": found[n]["fingerprint"]}
                                           for n in ("vdb", "vdc", "nvme0n1")]}, timeout=600)
        found = {d["name"]: d for d in json.loads(self.storage("list", {})[0])["disks"]}
        check(all((found[n]["state"], found[n]["blank"]) == ("free", True) for n in ("vdb", "vdc", "nvme0n1")),
              f"the disks were not given back blank: {found}")
        check(vm.run("ls /dev/mapper | grep cherry-pv") == "cherry-pv-vda1", "a released disk's volume is still open")
        vm.stop()

    def s15_boot_state(self):
        vm = self.vm
        disk = os.path.join(self.workdir, "sealed.img")
        run_qemu.make_disk(disk, 4 << 30)
        journal = "journalctl -b -u cherry-storage --no-pager -o cat"
        data, uki = pcrsign.read_uki(os.path.join(self.images, run_qemu.EFI_NAME))
        tmp = os.path.join(self.workdir, "boot-state")
        os.makedirs(tmp)

        # /var on a disk the agent claimed and gave to the pool cherry, as in 2: sealed to the image's policy.
        self.boot(disks=[disk])
        found = {d["name"]: d for d in json.loads(self.storage("list", {})[0])["disks"]}
        self.storage("claim", {"disks": [{"name": "vda", "fingerprint": found["vda"]["fingerprint"]}]}, timeout=600)
        self.storage("create_pool", {"name": "cherry", "disks": ["vda"]}, timeout=900)
        self.check_var_on_pool("vda1")
        self.check_sealed("vda1", uki)
        vm.run("echo boot-state >/var/cherry-smoke")
        vm.send("systemctl poweroff\n")
        vm.wait_exit(300)

        # The same image with one byte of its command line changed, which is measured, and the image signed anew
        # with another PCR key, which the disk's token does not name: the TPM unlocks the disk under neither, so the
        # pool is not seen, /var is in RAM, and the disk is left as it was.
        tampered = bytearray(data)
        cmdline = bytes(uki[".cmdline"])
        check(b"panic=10" in cmdline, f"no panic=10 on the command line to change: {cmdline}")
        tampered[pcrsign.pe_sections(data)[".cmdline"].offset + cmdline.index(b"panic=10") + 7] = ord("1")
        key = os.path.join(tmp, "other.key")
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", key],
                       check=True, capture_output=True)
        resigned = bytearray(data)
        pcrsign.write_pcrsig(resigned, json.dumps(pcrsign.entries(uki, pcrsign.public_key(key), key)))
        before = file_sha256(disk)
        for name, image in (("tampered", tampered), ("resigned", resigned)):
            images = os.path.join(tmp, name)
            os.makedirs(images)
            with open(os.path.join(images, run_qemu.EFI_NAME), "wb") as f:
                f.write(image)
            server = run_qemu.serve(images)
            try:
                self.boot(disks=[disk], server=server)
            finally:
                server.shutdown()
            shown = vm.run(journal)
            check("the TPM cannot unlock /dev/vda1" in shown, f"{name}: cherry-storage.service reported:\n{shown}")
            self.check_var_in_ram()
            found = {d["name"]: d for d in json.loads(self.storage("list", {})[0])["disks"]}
            check(found["vda"]["state"] == "locked", f"{name}: the disk is not locked: {found['vda']}")
            if name == "tampered":
                check("panic=11" in vm.run("cat /proc/cmdline"), "the changed command line was not booted")
                _, tampered_uki = pcrsign.read_uki(os.path.join(images, run_qemu.EFI_NAME))
                self.check_pcr11(tampered_uki, "sysinit:ready")
            else:
                self.check_pcr11(uki, "sysinit:ready")
            log(f"{name}: the TPM did not unlock the disk")
            vm.send("systemctl poweroff\n")
            vm.wait_exit(300)
        check(file_sha256(disk) == before, "the disk was written to under an image that could not unlock it")

        # Under the image itself, it does, and the files are there.
        self.boot(disks=[disk])
        self.check_var_on_pool("vda1")
        check(vm.run("cat /var/cherry-smoke") == "boot-state", "a file on /var did not survive")
        vm.stop()

    def s16_prod(self):
        """The prod image: the dev image's build with cherry.prod on its command line, which /init turns into root
        locked out (board/x86_64/rootfs-overlay/init). Nothing logs in, so this scenario watches the console: the
        pairing link shows, and the login prompt refuses root without asking for a password, as does the
        agetty.autologin credential, which the other scenarios get their shell through."""
        vm = self.vm
        prod = os.path.join(self.images, run_qemu.PROD_NAME)
        check(os.path.exists(prod), f"no {run_qemu.PROD_NAME} in {self.images}")
        for name, expected in ((run_qemu.PROD_NAME, True), (run_qemu.EFI_NAME, False)):
            cmdline = pcrsign.read_uki(os.path.join(self.images, name))[1][".cmdline"].rstrip(b"\0").decode()
            check((" cherry.prod" in f" {cmdline} ") == expected,
                  f"{name}'s command line {'lacks' if expected else 'has'} cherry.prod: {cmdline}")

        # Without credentials: the pairing link, and a login prompt that refuses root at once.
        run_qemu.ImageHandler.requests.clear()
        vm.start(name=run_qemu.PROD_NAME, credentials=[])
        vm.console.expect(r"openchamber://connect\?v=2&p=", 900, allow_boot=True)
        check(f"/{run_qemu.PROD_NAME}" in run_qemu.ImageHandler.requests,
              f"the firmware did not download the prod image: {run_qemu.ImageHandler.requests}")
        try:
            # The prompt may have come before the link, which agetty does not print again.
            vm.console.expect(r"login: $", 60)
        except Timeout:
            pass
        vm.send("root\n")
        m, _ = vm.console.expect(r"Login incorrect|Password: ?$", 60)
        check(m[0] == "Login incorrect", f"the prod image's console asked root for a password: {m[0]!r}")

        # With the autologin credential: refused all the same, and no shell answers.
        vm.start(name=run_qemu.PROD_NAME)
        vm.console.expect(r"login: root \(automatic login\)", 900, allow_boot=True)
        m, _ = vm.console.expect(r"Login incorrect|Password: ?$", 60)
        check(m[0] == "Login incorrect", f"the prod image's console asked autologin's root for a password: {m[0]!r}")
        vm.send("echo @@SH\"\"ELL@@\n")
        try:
            vm.console.expect(r"@@SHELL@@$", 15)
        except Timeout:
            pass
        else:
            check(False, "a shell answered on the prod image's console")
        log("the prod image refused root on the console, with the autologin credential and without")

    def run(self):
        names = scenario_names(type(self))
        results = []
        try:
            for i, name in enumerate(names, 1):
                log(f"=== {i}. {name[len(f's{i}_'):].replace('_', ' ')}")
                start = time.monotonic()
                outcome = "skipped" if getattr(self, name)() == SKIPPED else "passed"
                results.append(outcome)
                log(f"=== {i}. {outcome} in {time.monotonic() - start:.0f}s")
        finally:
            self.vm.stop()
            self.server.shutdown()
        skipped = results.count("skipped")
        log(f"{results.count('passed')} scenarios passed, {skipped} skipped "
            f"(serial log: {os.path.join(self.workdir, 'serial.log')})")


def main():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images", default=os.path.join(root, "output/cherry_x86_64/images"))
    parser.add_argument("--state", default=os.path.join(root, "output/cherry_x86_64/test"))
    args = parser.parse_args()
    log(f"timeout multiplier {MULT:g} ({'KVM' if run_qemu.kvm_usable() else 'TCG'})")
    try:
        Smoke(args).run()
    except TestFailure as e:
        log(f"FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
