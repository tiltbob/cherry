#!/usr/bin/env python3
"""End-to-end smoke test for Cherry: UEFI HTTP boot in QEMU + OVMF (stdlib only).

  1. the firmware HTTP boots the EFI binary; the root filesystem is the EROFS
     image from the initramfs, read-only; /var is a tmpfs, as the machine has
     no disk; the machine ID comes from the TPM's endorsement key (a swtpm on
     CRB), and the hostname from the machine ID; networking and sshd work, an
     SSH key passed as a credential is installed, no unit failed; the firewall
     was loaded before networkd started, lets IPv6 router advertisements in,
     and keeps sshd from answering outside the VM until a rule lets it in,
     which restarting the firewall takes back out
  2. a systemd-nspawn container runs with a veth link, and systemd-networkd
     sets up masquerading for it and can write its state files, even after
     systemd-tmpfiles ran in an arch-chroot of a root that numbers its users
     differently (as pacstrap's pacman hooks do); through the firewall, the
     container leases its address from networkd, pings the host and reaches
     out of the VM, but sshd on the host does not answer it
  3. a second boot starts from scratch again (nothing persists), except the
     machine's identity: the same TPM, now on TIS, gives the same machine ID
     and hostname
  4. the cherry user creates a Debian container with cherry-bootstrap@.service
     (debootstrap, as root), boots, stops and removes it with machinectl
     (skipped when the guest cannot reach deb.debian.org)
  5. rpmstrap installs Fedora into /var/lib/machines and the container boots
     (skipped when the guest cannot reach mirrors.fedoraproject.org)
  6. pacstrap installs Arch Linux into /var/lib/machines, the container's own
     pacman downloads in its Landlock sandbox, and the container boots
     (skipped when the guest cannot reach the Arch Linux mirrors)
  7. the OpenCode binary is the packaged version (stripped, it would be a bare
     Bun), and only the binary: no opencode user, state directory or service
  8. cherry.service runs OpenChamber on Bun as the cherry user, serving its web
     UI and running the packaged OpenCode as that user too, both on loopback
     only and under its system call filter; once it was online,
     cherry-connect-url.service showed the pairing link and its QR code on the
     console, as that user; the user's polkit privileges cover machinectl and
     cherry-bootstrap@ and nothing else; no openchamber user, state directory
     or service
  9. OpenCode reads its system configuration, /etc/opencode, and through the
     MCP server it names, as the cherry user in cherry.service's sandbox, a
     Debian image is built from a recipe (cherry-image@.service), booted with
     its network up, entered, stopped, and exported as a tarball and a raw
     disk image that systemd-nspawn runs, then removed (skipped when the guest
     cannot reach deb.debian.org)
  10. zram is the swap device, and under memory pressure tmpfs pages go to it,
     compressed, and come back intact
  11. without a TPM, boot stops at cherry-no-tpm.target with the error on the
     console: no network, no sshd, no OpenChamber, no containers
  12. on Secure Boot firmware in setup mode, cherry-secureboot.service enrolls
     the image's keys and the option ROM driver the firmware loaded (QEMU's
     iPXE for virtio-net) and reboots; the firmware then refuses the image,
     unsigned, but boots it with Secure Boot on once signed by a CA the test
     adds to db (skipped when the image has no keys)
  13. with disks, the machine makes its blank one its /var: a LUKS2 volume
     whose key the TPM seals, holding ext4, and leaves the disk with a
     partition table alone; after a poweroff, which unmounts it cleanly, the
     TPM unlocks it and /var kept its files and the SSH host key; another TPM
     cannot unlock it, so that machine's /var is a tmpfs and the disk stays as
     it was, until wipefs, as the README says, makes it blank again

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
TEST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPZ6tCIi2mVyuCoH1GzkcdXxsTSu6cAvpCOAGWT3d6JJ cherry-smoke"
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
        self.ssh_port = None
        self.n = 0

    def start(self, tpm_interface="crb", secure_boot=False, server=None, tpm="tpm", disks=()):
        """Boot with a TPM on tpm_interface, or None for no TPM, from server (default: the images'), with the given
        disk images. The TPM's state is <workdir>/<tpm>: the VM's own TPM, one for the whole test, unless another one
        is named. With secure_boot, on OVMF's Secure Boot build, whose variable store lasts from one start to the next
        (in setup mode at first), and QEMU exits when the guest reboots."""
        self.stop()
        url = run_qemu.boot_url(server or self.server)
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
        self.ssh_port = free_port()
        cmd = run_qemu.qemu_command(vars_path, serial="stdio", ssh_port=self.ssh_port,
                                    credentials=["agetty.autologin=root", f"ssh.authorized_keys.root={TEST_KEY}"],
                                    tpm=self.tpm, tpm_interface=tpm_interface, secure_boot=secure_boot,
                                    disks=disks)
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

    def ssh_answers(self):
        """Whether sshd's banner comes back from outside the VM, through QEMU's port forward. QEMU accepts the
        connection at once and only then connects to the guest: a dropped SYN shows as silence."""
        with socket.create_connection(("127.0.0.1", self.ssh_port), timeout=5 * MULT) as s:
            try:
                return s.recv(4) == b"SSH-"
            except TimeoutError:
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

    def boot(self, tpm_interface="crb", banner=None, secure_boot=False, server=None, tpm="tpm", disks=(),
             expect="running"):
        vm = self.vm
        run_qemu.ImageHandler.requests.clear()
        start = time.monotonic()
        vm.start(tpm_interface, secure_boot=secure_boot, server=server, tpm=tpm, disks=disks)
        if banner:
            vm.console.expect(re.escape(banner), 600, allow_boot=True)
        vm.wait_shell()
        took = time.monotonic() - start
        check(f"/{run_qemu.EFI_NAME}" in run_qemu.ImageHandler.requests,
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

    def check_firewall(self):
        """The firewall was loaded before systemd-networkd started, lets the router advertisements of QEMU's network
        in (they give the host an IPv6 address), and keeps sshd from answering outside the VM until a rule lets it
        in; restarting the firewall takes that rule back out."""
        vm = self.vm
        check(vm.run("systemctl is-active cherry-firewall.service") == "active", "the firewall is not loaded")
        loaded, started = (int(vm.run(f"systemctl show -p {p} --value {unit}")) for p, unit in
                           (("ActiveEnterTimestampMonotonic", "cherry-firewall"),
                            ("InactiveExitTimestampMonotonic", "systemd-networkd")))
        check(loaded <= started, "systemd-networkd started before the firewall was loaded")
        vm.run("for i in $(seq 60); do ip -6 addr show | grep -q 'inet6 fec0:' && exit 0; sleep 1; done; "
               "ip -6 addr show; exit 1", timeout=90)
        check(not vm.ssh_answers(), "sshd answers outside the VM, through the firewall")
        vm.run("nft add rule inet cherry input tcp dport 22 accept")
        check(vm.ssh_answers(), "sshd does not answer outside the VM once a rule lets it in")
        vm.run("systemctl restart cherry-firewall.service")
        check(not vm.ssh_answers(), "restarting the firewall did not take the rule for sshd back out")

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

    def check_var_on_disk(self, partition):
        """/var is the ext4 of cherry-var, the LUKS2 volume on the given partition, which is labelled cherry-var."""
        vm = self.vm
        var = vm.fstype("/var")
        check(" - ext4 " in var, f"/var is not an ext4: {var}")
        check("nosuid" not in var.split(" - ")[0], f"/var is nosuid, which breaks setuid in containers: {var}")
        dm = vm.run("grep -lx cherry-var /sys/class/block/dm-*/dm/name | cut -d / -f 5")
        check(var.split()[2] == vm.run(f"cat /sys/class/block/{dm}/dev"), f"/var is not on {dm}, cherry-var: {var}")
        check(vm.run(f"cat /sys/class/block/{dm}/dm/uuid").startswith("CRYPT-LUKS2-"), "cherry-var is not LUKS2")
        check(vm.run(f"ls /sys/class/block/{dm}/slaves") == partition, f"cherry-var is not on {partition}")
        vm.run(f"grep -qx PARTNAME=cherry-var /sys/class/block/{partition}/uevent")

    def s1_http_boot(self):
        vm = self.vm
        self.boot()

        root = vm.fstype("/")
        check(re.match(r"\S+ \S+ \S+ / / ro[ ,]", root), f"/ is not mounted read-only: {root}")
        check(" - erofs /rootfs.erofs " in root, f"/ is not the EROFS image from the initramfs: {root}")
        rc, _ = vm.run("touch /cherry-rw-test", check_rc=False)
        check(rc != 0, "the root filesystem is writable")
        var = vm.fstype("/var")
        check(" - tmpfs " in var, f"/var is not a tmpfs: {var}")
        check("nosuid" not in var.split(" - ")[0], f"/var is nosuid, which breaks setuid in containers: {var}")
        vm.run("journalctl -b -u cherry-var --no-pager | grep -q 'no partition labelled cherry-var and no blank disk'")
        check(vm.run("readlink -f /root") == "/var/roothome", "/root is not on /var")
        check("NAME=Cherry" in vm.run("cat /etc/os-release"), "unexpected os-release")
        check("console=ttyS0" in vm.run("cat /proc/cmdline"), "the UKI command line was not used")
        self.machine_id = self.check_identity("crb")
        failed = vm.run("systemctl --failed --no-legend --plain")
        check(failed == "", f"failed units:\n{failed}")
        check(vm.run("systemctl is-active sshd.service") == "active", "sshd is not running")
        vm.run("test -s /var/lib/sshd/etc/ssh/ssh_host_ed25519_key.pub")
        check(vm.run("cat /root/.ssh/authorized_keys") == TEST_KEY,
              "the ssh.authorized_keys.root credential was not applied")
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

    def s2_nspawn(self):
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
        # reaches out of the VM, masqueraded, to the HTTP server that served the image; but sshd on the host
        # answers it neither on its link, over IPv4 or IPv6, nor on the host's own address, though it answers the
        # host itself. Commands run with the host's tools in the container's network namespace.
        inside = ("systemd-run -q --wait --pipe --collect "
                  "-p NetworkNamespacePath=/proc/$(machinectl show smoke -p Leader --value)/ns/net")

        def sshd_answers(address, prefix=""):
            rc, out = vm.run(f"{prefix} timeout {5 * MULT:g} bash -c 'exec 3<>/dev/tcp/{address}/22 && head -c 4 <&3'",
                             timeout=60, check_rc=False)
            return rc == 0 and out == "SSH-"

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
        for target in (address, "::1"):
            check(sshd_answers(target), f"sshd does not answer the host itself on {target}")
        for target in (gateway, address, f"{local}%host0"):
            check(not sshd_answers(target, inside), f"sshd answers the container on {target}, through the firewall")
        vm.run("machinectl terminate smoke; for i in $(seq 30); do machinectl show smoke >/dev/null 2>&1 || exit 0; "
               "sleep 1; done; exit 1", timeout=60)

    def s3_stateless_reboot(self):
        vm = self.vm
        vm.run("touch /var/cherry-smoke")
        # The same TPM state, attached through the other interface.
        self.boot(tpm_interface="tis")
        rc, _ = vm.run("test -e /var/cherry-smoke", check_rc=False)
        check(rc != 0, "/var survived a reboot")
        check(self.check_identity("tis") == self.machine_id, "the machine ID changed across a reboot")

    def s4_debootstrap(self):
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

    def s5_rpmstrap(self):
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

    def s6_pacstrap(self):
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
        # As in s4_debootstrap: `systemctl -M` fails until the container's bus is up, so wait for
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

    def s7_opencode(self):
        vm = self.vm
        version = package_version("opencode")
        # Stripped by Buildroot, the Bun single-file executable would run as a bare Bun and print Bun's version.
        check(vm.run("opencode --version") == f"opencode v{version}",
              f"opencode --version is not the packaged {version}")
        # Its grep tool is the image's ripgrep, and git is on the image for its snapshots and worktrees.
        vm.run("test -x /usr/bin/rg && test -x /usr/bin/git")
        # Only the binary: the opencode user, /var/lib/opencode and opencode.service come with
        # BR2_PACKAGE_OPENCODE_SERVICE, off in Cherry. OpenChamber runs the server, as the cherry user (s8).
        rc, _ = vm.run("getent passwd opencode", check_rc=False)
        check(rc != 0, "there is an opencode user")
        rc, _ = vm.run("test -e /var/lib/opencode || test -e /usr/lib/systemd/system/opencode.service", check_rc=False)
        check(rc != 0, "/var/lib/opencode or opencode.service is on the image")

    def s8_cherry(self):
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

    def mcp(self, tool, arguments, timeout=120):
        """Call a tool of OpenCode's MCP server, /etc/opencode's machines, as OpenCode in cherry.service would:
        the reply's text, which must not be an error."""
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
                          "/usr/bin/bun /usr/lib/cherry/machines-mcp.js </tmp/mcp-requests", timeout=timeout)
        for line in out.splitlines():
            if line.startswith("{") and json.loads(line).get("id") == 2:
                result = json.loads(line)["result"]
                text = result["content"][0]["text"]
                check(not result["isError"], f"the MCP tool {tool} failed:\n{text}")
                return text
        raise TestFailure(f"no reply from the MCP tool {tool}:\n{out}")

    def s9_images(self):
        vm = self.vm
        mirror = "http://deb.debian.org/debian"
        # As in s4_debootstrap.
        rc, out = vm.run(f"for i in $(seq 12); do wget -q -T 10 -t 1 -O /dev/null {mirror}/dists/trixie/InRelease "
                         "&& exit 0; sleep 5; done; resolvectl query deb.debian.org; exit 1",
                         timeout=240, check_rc=False)
        if rc != 0:
            log(f"skipped: the guest cannot reach {mirror}: {out}")
            return SKIPPED
        # OpenCode's system configuration names the MCP server; the skill comes with it. (A home of its own, apart
        # from the OpenCode that OpenChamber runs.)
        config = vm.run(f"{AS_CHERRY} -p PrivateTmp=yes -E HOME=/tmp opencode debug config")
        check('"path": "/etc/opencode/opencode.json"' in config and "machines-mcp.js" in config,
              f"OpenCode does not read /etc/opencode/opencode.json:\n{config}")
        vm.run("test -s /etc/opencode/skills/machine-images.md")

        name, root = "smoke-image", "/var/lib/machines/smoke-image"
        motd = "built by cherry-image for the smoke test"
        text = self.mcp("build", {
            "name": name, "distribution": "debian", "release": "trixie", "packages": ["iproute2"],
            "files": [{"path": "/etc/motd", "content": motd + "\n"},
                      {"path": "/usr/bin/smoke-hello", "mode": "0755",
                       "content": "#!/bin/sh\necho hello from $(cat /proc/sys/kernel/hostname)\n"}],
            "postinst": "systemctl enable systemd-networkd\necho postinst ran >/var/postinst-ran",
            "nspawn": "[Exec]\nEnvironment=SMOKE=1"}, timeout=1800)
        log("build:\n" + text)
        check(f"built {root} " in text, f"the build's log does not end as it should:\n{text}")
        check(vm.run(f"stat -c %a /var/log/cherry-image/build:{name}.log") == "644",
              "the build log is not readable by the cherry user")
        check(vm.run(f"stat -c %U /var/lib/cherry/images/{name}/image.conf") == "cherry",
              "the recipe was not written as the cherry user")
        check(vm.run(f"cat {root}.nspawn") == "[Exec]\nEnvironment=SMOKE=1", "the image's settings were not installed")
        vm.run(f"test -e {root}/etc/machine-id && ! test -s {root}/etc/machine-id && "
               f"test -z \"$(ls {root}/var/lib/apt/lists)\" && ! test -e {root}/.cherry-image")
        log(f"{name}: " + vm.run(f"du -sh {root} | cut -f 1"))

        check(self.mcp("start", {"name": name}, timeout=300) == f"{name} is running", f"{name} did not start")
        # The command runs in a login session: its messages may come first.
        text = self.mcp("exec", {"name": name, "command": "cat /etc/motd /var/postinst-ran; smoke-hello"})
        check(text.startswith("exit status 0\n") and text.endswith(f"{motd}\npostinst ran\nhello from {name}"),
              f"exec in {name} gave:\n{text}")
        text = self.mcp("exec", {"name": name, "command": "systemctl status systemd-networkd; exit 3"})
        check(text.startswith("exit status 3\n") and "Active: active" in text,
              f"exec does not report the exit status, or systemctl paged:\n{text}")
        # Its network: host0, configured by the image's networkd with DHCP from the host.
        text = self.mcp("exec", {"name": name, "command": "for i in $(seq 60); do ip -4 -br addr show host0 | "
                                 "grep -q / && break; sleep 1; done; ip -4 -br addr show host0"}, timeout=180)
        check(re.search(r"^host0\S*\s+UP\s+\d+\.\d+\.\d+\.\d+/\d+", text, re.M), f"host0 has no address:\n{text}")
        check(self.mcp("stop", {"name": name}, timeout=180) == f"{name} is stopped", f"{name} did not stop")

        # The tarball: owned by the cherry user, the image's tree, which systemd-nspawn runs once unpacked.
        export = json.loads(self.mcp("export", {"name": name, "format": "tar"}, timeout=900))
        tarball = export["path"]
        check(tarball == f"/var/lib/cherry/exports/{name}.tar.xz", f"unexpected export: {export}")
        check(vm.run(f"stat -c %U {tarball}") == "cherry", "the tarball is not the cherry user's")
        check(vm.run(f"sha256sum {tarball} | cut -d ' ' -f 1") == export["sha256"], "the tarball's SHA-256 is wrong")
        copy = "/var/lib/machines/smoke-copy"
        vm.run(f"mkdir {copy} && tar --numeric-owner --xattrs -C {copy} -xJf {tarball} && rm {tarball}", timeout=300)
        check(vm.run(f"systemd-nspawn -q --pipe --register=no -D {copy} cat /etc/motd") == motd,
              "the unpacked tarball does not run")
        vm.run(f"rm -rf {copy}")
        # The raw image: a disk image that systemd-nspawn runs as is.
        raw = json.loads(self.mcp("export", {"name": name, "format": "raw"}, timeout=900))["path"]
        check(vm.run(f"systemd-nspawn -q --pipe --register=no --image={raw} cat /etc/motd") == motd,
              "the raw image does not run")
        log(f"exported {name}: " + vm.run(f"du -h --apparent-size {raw} | cut -f 1") + " raw, sparse in "
            + vm.run(f"du -h {raw} | cut -f 1"))

        check(self.mcp("remove", {"name": name}) == f"{name} removed", f"{name} was not removed")
        rc, _ = vm.run(f"test -e {root} || test -e {root}.nspawn", check_rc=False)
        check(rc != 0, "the image or its settings are still there")
        vm.run(f"rm -rf /var/lib/cherry/images/{name} {raw}")

    def s10_zram(self):
        vm = self.vm
        mem_kb = int(vm.run("awk '/^MemTotal:/ { print $2 }' /proc/meminfo"))
        swaps = vm.run("tail -n +2 /proc/swaps")
        fields = swaps.split()
        # Size in kB: all of zram0 but the page that holds the swap header.
        check(len(fields) == 5 and fields[0] == "/dev/zram0" and abs(int(fields[2]) - mem_kb) <= 8,
              f"zram0 is not the only swap device, as large as the RAM ({mem_kb} kB):\n{swaps}")
        check("[zstd]" in vm.run("cat /sys/block/zram0/comp_algorithm"), "zram does not compress with zstd")
        check(vm.run("cat /proc/sys/vm/page-cluster") == "0", "vm.page-cluster is not 0")
        # Write 400 MiB of text to a tmpfs from a cgroup limited to 128 MiB: its own shmem pages
        # have to go to zram, compressed.
        size = 400 << 20
        data = f"yes cherry-zram | head -c {size}"
        vm.run(f"systemd-run --wait -q -p MemoryMax=128M sh -c '{data} >/var/tmp/zram-test'", timeout=300)
        stats = vm.run("cat /sys/block/zram0/mm_stat").split()
        stored, compressed = int(stats[0]), int(stats[1])
        check(stored >= 200 << 20, f"only {stored} bytes went to zram under memory pressure: {stats}")
        log(f"zram holds {stored >> 20} MiB in {compressed >> 20} MiB")
        check(vm.run("md5sum </var/tmp/zram-test") == vm.run(f"{data} | md5sum"),
              "data that went through zram came back changed")
        vm.run("rm /var/tmp/zram-test")

    def s11_no_tpm(self):
        vm = self.vm
        self.boot(tpm_interface=None, banner=NO_TPM_BANNER)
        error = vm.run("cat /run/cherry/identity/error")
        check(error == "no TPM 2.0 found", f"unexpected identity error: {error}")
        rc, _ = vm.run("test -e /run/cherry/identity/ek-hash", check_rc=False)
        check(rc != 0, "an EK hash without a TPM")
        check(vm.run("systemctl is-active cherry-no-tpm.target") == "active", "cherry-no-tpm.target is not active")
        rc, states = vm.run("systemctl is-active multi-user.target sshd.service systemd-networkd.service "
                            "cherry.service machines.target", check_rc=False)
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

    def s12_secure_boot(self):
        vm = self.vm
        keys = "/usr/share/cherry/secureboot"
        rc, _ = vm.run(f"test -s {keys}/PK.auth", check_rc=False)
        if rc != 0:
            log("skipped: the image has no Secure Boot keys (scripts/secureboot.py init writes them)")
            return SKIPPED
        for tool in ("openssl", "sbsign"):
            check(shutil.which(tool), f"{tool} not found: apt install openssl sbsigntool")
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

        # Secure Boot is on: the image, unsigned, is refused.
        vm.start(secure_boot=True)
        vm.console.expect(r"Access Denied|Security Violation", 600, allow_boot=True)
        vm.stop()

        # Signed by a CA added to db, it boots, with Secure Boot on.
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
        flags = vm.run("cd /sys/firmware/efi/efivars && for v in SecureBoot SetupMode; do "
                       "printf '%s=%s ' $v $(od -An -tu1 -j4 -N1 $v-8be4df61-93ca-11d2-aa0d-00e098032b8c); done")
        check(flags.split() == ["SecureBoot=1", "SetupMode=0"], f"not booted with Secure Boot on: {flags}")
        report = vm.run("journalctl -b -u cherry-secureboot --no-pager -o cat")
        check("Secure Boot is on, with Cherry's keys." in report, f"cherry-secureboot reported:\n{report}")
        rc, _ = vm.run("ls /sys/firmware/efi/efivars/CherrySecureBootEnrolled-*", check_rc=False)
        check(rc != 0, "the enrollment mark was not cleared")
        check(vm.run("systemctl --failed --no-legend --plain") == "", "units failed with Secure Boot on")

    def s13_encrypted_var(self):
        vm = self.vm
        # vda has a partition table, so it is in use; vdb is blank. Both are large enough to be claimed.
        used, blank = os.path.join(self.workdir, "used.img"), os.path.join(self.workdir, "blank.img")
        run_qemu.make_disk(used, 1 << 30)
        with open(used, "r+b") as f:
            f.write(DOS_MBR)
        run_qemu.make_disk(blank, 4 << 30)
        used_sha256 = file_sha256(used)
        disks = [used, blank]
        journal = "journalctl -b -u cherry-var --no-pager -o cat"

        # The machine makes the blank disk its encrypted /var, sealed by its TPM.
        self.boot(disks=disks)
        shown = vm.run(journal)
        log("cherry-var.service:\n" + shown)
        check("making the blank disk /dev/vdb " in shown, "cherry-var.service did not set up vdb")
        self.check_var_on_disk("vdb1")
        luks = vm.run("cryptsetup luksDump /dev/vdb1")
        check(re.search(r"^\s+\d+: systemd-tpm2$", luks, re.M), f"no systemd-tpm2 token on /dev/vdb1:\n{luks}")
        check(len(re.findall(r"^\s+\d+: luks2$", luks, re.M)) == 1, f"not one key slot, the TPM's:\n{luks}")
        vm.run("echo cherry-smoke >/var/cherry-smoke")
        host_key = vm.run("cat /var/lib/sshd/etc/ssh/ssh_host_ed25519_key.pub")
        # Powered off, not pulled: /var is unmounted and the volume closed on the way down.
        vm.send("systemctl poweroff\n")
        vm.wait_exit(300)

        # The TPM unlocks it at the next boot, its filesystem was left clean, and /var kept its files.
        self.boot(disks=disks)
        shown = vm.run(journal)
        check("/var is on /dev/vdb1, unlocked with the TPM" in shown and "making" not in shown,
              f"cherry-var.service did not unlock vdb1 alone:\n{shown}")
        check("recovering journal" not in shown, f"/var was not unmounted cleanly at the poweroff:\n{shown}")
        self.check_var_on_disk("vdb1")
        check(vm.run("cat /var/cherry-smoke") == "cherry-smoke", "a file on /var did not survive the reboot")
        check(vm.run("cat /var/lib/sshd/etc/ssh/ssh_host_ed25519_key.pub") == host_key, "the SSH host key changed")

        # Another machine's TPM cannot: its /var is a tmpfs, and the disk is left as it was.
        self.boot(tpm="tpm-other", disks=[blank], expect="degraded")
        check(vm.run("systemctl --failed --no-legend --plain | awk '{ print $1 }'") == "cherry-var.service",
              "cherry-var.service is not the one failed unit")
        vm.run(f"{journal} | grep -q 'the TPM cannot unlock /dev/vda1'")
        check(" - tmpfs " in vm.fstype("/var"), "/var is not a tmpfs on another machine")
        check(re.search(r"^\s+\d+: systemd-tpm2$", vm.run("cryptsetup luksDump /dev/vda1"), re.M),
              "the disk changed on another machine")
        # Erased as the README says, it is blank again: no partition, and nothing wipefs finds.
        vm.run("wipefs --all --force /dev/vda1 /dev/vda")
        check(vm.run("wipefs --no-act --noheadings /dev/vda; ls -d /sys/class/block/vda/vda* 2>/dev/null; true") == "",
              "wipefs did not leave the disk blank")
        vm.stop()
        check(file_sha256(used) == used_sha256, "the disk with a partition table was written to")

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
