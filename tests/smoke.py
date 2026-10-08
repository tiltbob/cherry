#!/usr/bin/env python3
"""End-to-end smoke test for Cherry: UEFI HTTP boot in QEMU + OVMF (stdlib only).

  1. the firmware HTTP boots the EFI binary; the root filesystem is the EROFS
     image from the initramfs, read-only; /var is a tmpfs; the machine ID
     comes from the TPM's endorsement key (a swtpm on CRB); networking and
     sshd work, an SSH key passed as a credential is installed, no unit failed
  2. a systemd-nspawn container runs with a veth link, and systemd-networkd
     sets up masquerading for it and can write its state files, even after
     systemd-tmpfiles ran in an arch-chroot of a root that numbers its users
     differently (as pacstrap's pacman hooks do)
  3. a second boot starts from scratch again (nothing persists), except the
     machine's identity: the same TPM, now on TIS, gives the same machine ID
     and the same IPFS peer ID
  4. the cherry user creates a Debian container with cherry-bootstrap@.service
     (debootstrap, as root), boots, stops and removes it with machinectl
     (skipped when the guest cannot reach deb.debian.org)
  5. rpmstrap installs Fedora into /var/lib/machines and the container boots
     (skipped when the guest cannot reach mirrors.fedoraproject.org)
  6. pacstrap installs Arch Linux into /var/lib/machines, the container's own
     pacman downloads in its Landlock sandbox, and the container boots
     (skipped when the guest cannot reach the Arch Linux mirrors)
  7. the IPFS daemon runs as its own user, a login shell's ipfs commands reach
     it, and content added through its API comes back through its gateway,
     which listens on loopback only
  8. the OpenCode binary is the packaged version (stripped, it would be a bare
     Bun), and only the binary: no opencode user, state directory or service
  9. cherry.service runs OpenChamber on Bun as the cherry user, on loopback
     only (no password was given), serving its web UI and running the packaged
     OpenCode as that user too; once it was online, cherry-connect-url.service
     showed the pairing link and its QR code on the console, as that user; the
     user's polkit privileges cover machinectl and cherry-bootstrap@ and
     nothing else; no openchamber user, state directory or service
  10. zram is the swap device, and under memory pressure tmpfs pages go to it,
     compressed, and come back intact
  11. without a TPM, boot stops at cherry-no-tpm.target with the error on the
     console: no network, no sshd, no IPFS, no OpenChamber, no containers

Timeouts scale with CHERRY_TEST_TIMEOUT_MULT (default 1 with KVM, 4 without).
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import run_qemu  # noqa: E402

MULT = float(os.environ.get("CHERRY_TEST_TIMEOUT_MULT", "1" if run_qemu.kvm_usable() else "4"))

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|[=>78cDEHM])")
BOOT = re.compile(r'BdsDxe: starting Boot([0-9A-F]{4}) "([^"]*)"')
PANIC = re.compile(r"Kernel panic - not syncing")
# DER SubjectPublicKeyInfo of an uncompressed NIST P-256 public key, up to the point.
P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d03010703420004")
NO_TPM_BANNER = "Cherry stopped: no usable TPM 2.0."
# Run a command as the cherry user, from the root shell on the console.
AS_CHERRY = "systemd-run -q --wait --pipe --collect -p User=cherry"
TEST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPZ6tCIi2mVyuCoH1GzkcdXxsTSu6cAvpCOAGWT3d6JJ cherry-smoke"
FATAL = [
    (re.compile(r"No bootable option"), "the firmware found nothing to boot"),
    (re.compile(r'BdsDxe: starting Boot[0-9A-F]{4} "EFI Internal Shell'), "the firmware fell through to the UEFI shell"),
    (re.compile(r"^Shell> ", re.M), "the firmware started the UEFI shell"),
    (re.compile(r"Found ordering cycle"), "systemd found an ordering cycle"),
    (PANIC, "kernel panic"),
]


SKIPPED = "skipped"


def package_version(name):
    """The version of one of Cherry's packages, from package/<name>/<name>.mk."""
    mk = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "package", name, f"{name}.mk")
    with open(mk) as f:
        return re.search(rf"^{name.upper()}_VERSION = (\S+)$", f.read(), re.M).group(1)


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
        self.n = 0

    def start(self, tpm_interface="crb"):
        """Boot with the VM's TPM (one swtpm state for the whole test) on tpm_interface, or None for no TPM."""
        self.stop()
        vars_path = os.path.join(self.workdir, "OVMF_VARS.fd")
        run_qemu.write_vars(vars_path, run_qemu.boot_url(self.server))
        if tpm_interface:
            self.tpm = run_qemu.Swtpm(os.path.join(self.workdir, "tpm"))
        cmd = run_qemu.qemu_command(vars_path, serial="stdio", credentials=["agetty.autologin=root",
                                                                             f"ssh.authorized_keys.root={TEST_KEY}"],
                                    tpm=self.tpm, tpm_interface=tpm_interface)
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
            details = self.run("systemctl --failed --no-legend --plain; journalctl -b -p warning --no-pager | tail -n 60",
                               check_rc=False)[1]
            log(f"system state {state!r}:\n{details}")
        return state

    def fstype(self, mountpoint):
        return self.run(f"awk '$5 == \"{mountpoint}\" {{ t = $0 }} END {{ print t }}' /proc/self/mountinfo")


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

    def boot(self, tpm_interface="crb", banner=None):
        vm = self.vm
        run_qemu.ImageHandler.requests.clear()
        start = time.monotonic()
        vm.start(tpm_interface)
        if banner:
            vm.console.expect(re.escape(banner), 600, allow_boot=True)
        vm.wait_shell()
        took = time.monotonic() - start
        check(f"/{run_qemu.EFI_NAME}" in run_qemu.ImageHandler.requests,
              f"the firmware did not download the EFI binary over HTTP: {run_qemu.ImageHandler.requests}")
        log(f"HTTP booted to a shell in {took:.0f}s")
        state = vm.settle()
        check(state == "running", f"system is {state!r}, not running")

    def check_networkd_state(self):
        """systemd-networkd still owns /run/systemd/netif and never failed to write its state files there."""
        vm = self.vm
        owners = vm.run("stat -c '%n %U:%G' /run/systemd/netif /run/systemd/netif/links /run/systemd/netif/leases")
        check(all(line.endswith(" systemd-network:systemd-network") for line in owners.splitlines()),
              f"systemd-network no longer owns its runtime directories:\n{owners}")
        rc, errors = vm.run("journalctl -b -u systemd-networkd --no-pager | grep -E 'Failed to update .*state file'",
                            check_rc=False)
        check(rc == 1, "systemd-networkd could not write its state files:\n" + "\n".join(errors.splitlines()[-5:]))

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
        # The TPM's own EK: creating it again gives the same key.
        vm.run("tpm2_createek -T device:/dev/tpmrm0 -G ecc -c /tmp/ek.ctx -f der -u /tmp/ek.der >/dev/null && "
               "cmp /tmp/ek.der /run/cherry/identity/ek.der; rc=$?; rm -f /tmp/ek.ctx /tmp/ek.der; exit $rc")
        rc, error = vm.run("cat /run/cherry/identity/error", check_rc=False)
        check(rc != 0, f"an identity error was recorded: {error}")
        log(f"machine identity: EK hash {ek_hash} via {driver}")
        return machine_id

    def ipfs_peer_id(self):
        """The IPFS daemon runs with the key that ipfs-identity.service derived from the TPM; return its peer ID."""
        vm = self.vm
        peer_id = vm.run("ipfs id -f '<id>'")
        derived = vm.run("journalctl -b -u ipfs-identity --no-pager -o cat "
                         "| sed -n 's/^IPFS peer ID \\([^ ]*\\).*/\\1/p' | tail -n 1")
        check(peer_id == derived, f"the IPFS daemon's peer ID {peer_id} is not the TPM-derived one: {derived!r}")
        return peer_id

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
        self.peer_id = self.ipfs_peer_id()
        vm.run("for i in $(seq 60); do ip -4 addr show | grep -q 'inet 10\\.0\\.2\\.' && exit 0; sleep 1; done; "
               "networkctl; exit 1", timeout=90)
        # systemd arms it before journald runs, so its message is only in the kernel log.
        vm.run("test -c /dev/watchdog0 && dmesg | grep -q 'Using hardware watchdog'")
        check(vm.run("systemctl show -p RuntimeWatchdogUSec --value") == "30s", "RuntimeWatchdogSec not applied")
        log("memory of the booted OS, before any container:\n" + vm.run(
            "free -k; df -k /var /run | sed 's/^/  /'; "
            "grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|Shmem|Slab|KernelStack|PageTables):' /proc/meminfo"))

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
        check(self.ipfs_peer_id() == self.peer_id, "the IPFS peer ID changed across a reboot")

    def s4_debootstrap(self):
        vm = self.vm
        mirror = "http://deb.debian.org/debian"
        # systemd-resolved can fail lookups for a while after boot (DNSSEC, DoT probing), so retry.
        rc, out = vm.run(f"for i in $(seq 12); do wget -q -T 10 -t 1 -O /dev/null {mirror}/dists/trixie/InRelease "
                         "&& exit 0; sleep 5; done; resolvectl query deb.debian.org; exit 1", timeout=240, check_rc=False)
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

    def s7_ipfs(self):
        vm = self.vm
        check(vm.run("systemctl is-active ipfs.service") == "active", "ipfs.service is not running")
        pid = vm.run("systemctl show -p MainPID --value ipfs.service")
        check(vm.run(f"stat -c %U /proc/{pid}") == "ipfs", "the IPFS daemon does not run as the ipfs user")
        check(vm.run("echo $IPFS_PATH") == "/var/lib/ipfs", "login shells don't set IPFS_PATH to the daemon's repository")
        check(vm.run("stat -c %U $IPFS_PATH/config") == "ipfs", "the IPFS repository does not belong to the ipfs user")
        # Through the daemon's RPC API: the CLI finds its address in $IPFS_PATH/api.
        peer_id = vm.run("ipfs id -f '<id>'")
        check(re.fullmatch(r"12D3KooW[1-9A-HJ-NP-Za-km-z]+", peer_id), f"unexpected peer ID {peer_id!r}")
        cid = vm.run("echo cherry-smoke | ipfs add -Q")
        check(vm.run(f"ipfs cat {cid}") == "cherry-smoke", "ipfs cat does not return what ipfs add stored")
        check(vm.run(f"wget -q -O - http://127.0.0.1:8080/ipfs/{cid}") == "cherry-smoke",
              "the gateway does not serve what ipfs add stored")
        address = vm.run("ip -4 addr show scope global | sed -n 's/.* inet \\([0-9.]*\\)\\/.*/\\1/p' | head -n 1")
        rc, _ = vm.run(f"wget -q -T 5 -t 1 -O /dev/null http://{address}:8080/ipfs/{cid}", check_rc=False)
        check(rc != 0, f"the gateway answers on {address}, not only on loopback")
        # QUIC gets the socket buffers it asks for.
        check(vm.run("cat /proc/sys/net/core/rmem_max") == "7500000", "net.core.rmem_max is not raised for QUIC")
        rc, _ = vm.run("journalctl -b -u ipfs --no-pager | grep -q 'failed to sufficiently increase'", check_rc=False)
        check(rc == 1, "QUIC could not get the socket buffers it asked for")
        # Peers need outbound network, so only report them.
        peers = vm.run("for i in $(seq 30); do n=$(ipfs swarm peers | wc -l); [ $n -gt 0 ] && break; sleep 1; done; "
                       "echo $n", timeout=90)
        log(f"IPFS peer ID {peer_id}, {peers} swarm peers")

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
        rc, out = vm.run(f"{AS_CHERRY} systemctl start cherry-connect-url.service", check_rc=False)
        check(rc != 0 and ("denied" in out.lower() or "authentication" in out.lower()),
              f"the cherry user may start units other than cherry-bootstrap@: {out}")
        # Everything in the service, OpenCode included, runs as the cherry user.
        users = vm.run("for p in $(cat /sys/fs/cgroup/system.slice/cherry.service/cgroup.procs); do "
                       "stat -c %U /proc/$p 2>/dev/null; done | sort -u")
        check(users == "cherry", f"cherry.service has processes of other users: {users}")
        check("OpenChamber" in vm.run(f"wget -q -O - {url}/"), "the web UI is not served")
        address = vm.run("ip -4 addr show scope global | sed -n 's/.* inet \\([0-9.]*\\)\\/.*/\\1/p' | head -n 1")
        rc, _ = vm.run(f"wget -q -T 5 -t 1 -O /dev/null http://{address}:3000/health", check_rc=False)
        check(rc != 0, f"OpenChamber answers on {address}, not only on loopback, without a UI password")
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
                            "ipfs-identity.service ipfs.service cherry.service "
                            "machines.target", check_rc=False)
        check(set(states.split()) == {"inactive"}, f"units started without a TPM:\n{states}")
        failed = vm.run("systemctl --failed --no-legend --plain")
        check(failed == "", f"failed units:\n{failed}")
        vm.run("journalctl -b -u cherry-no-tpm.service --no-pager | grep -q 'Stopped: no usable TPM 2.0'")

    def run(self):
        scenarios = [self.s1_http_boot, self.s2_nspawn, self.s3_stateless_reboot, self.s4_debootstrap,
                     self.s5_rpmstrap, self.s6_pacstrap, self.s7_ipfs, self.s8_opencode, self.s9_openchamber,
                     self.s10_zram, self.s11_no_tpm]
        results = []
        try:
            for i, scenario in enumerate(scenarios, 1):
                name = scenario.__name__[len(f"s{i}_"):].replace("_", " ")
                log(f"=== {i}. {name}")
                start = time.monotonic()
                outcome = "skipped" if scenario() == SKIPPED else "passed"
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
