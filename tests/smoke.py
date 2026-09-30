#!/usr/bin/env python3
"""End-to-end smoke test for Cherry: UEFI HTTP boot in QEMU + OVMF (stdlib only).

  1. the firmware HTTP boots the EFI binary; systemd runs straight from the
     initramfs, which is remounted read-only; /var is a tmpfs; networking and
     sshd work, an SSH key passed as a credential is installed, no unit failed
  2. a systemd-nspawn container runs with a veth link, and systemd-networkd
     sets up masquerading for it
  3. a second boot starts from scratch again (nothing persists)
  4. debootstrap installs Debian into /var/lib/machines and the container boots
     (skipped when the guest cannot reach deb.debian.org)

Timeouts scale with CHERRY_TEST_TIMEOUT_MULT (default 1 with KVM, 4 without).
"""

import argparse
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
TEST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPZ6tCIi2mVyuCoH1GzkcdXxsTSu6cAvpCOAGWT3d6JJ cherry-smoke"
FATAL = [
    (re.compile(r"No bootable option"), "the firmware found nothing to boot"),
    (re.compile(r'BdsDxe: starting Boot[0-9A-F]{4} "EFI Internal Shell'), "the firmware fell through to the UEFI shell"),
    (re.compile(r"^Shell> ", re.M), "the firmware started the UEFI shell"),
    (re.compile(r"Found ordering cycle"), "systemd found an ordering cycle"),
    (PANIC, "kernel panic"),
]


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
        self.console = None
        self.n = 0

    def start(self):
        self.stop()
        vars_path = os.path.join(self.workdir, "OVMF_VARS.fd")
        run_qemu.write_vars(vars_path, run_qemu.boot_url(self.server))
        cmd = run_qemu.qemu_command(vars_path, serial="stdio", credentials=["agetty.autologin=root",
                                                                             f"ssh.authorized_keys.root={TEST_KEY}"])
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

    def boot(self):
        vm = self.vm
        run_qemu.ImageHandler.requests.clear()
        vm.start()
        took = vm.wait_shell()
        check(f"/{run_qemu.EFI_NAME}" in run_qemu.ImageHandler.requests,
              f"the firmware did not download the EFI binary over HTTP: {run_qemu.ImageHandler.requests}")
        log(f"HTTP booted to a shell in {took:.0f}s")
        state = vm.settle()
        check(state == "running", f"system is {state!r}, not running")

    def s1_http_boot(self):
        vm = self.vm
        self.boot()

        root = vm.fstype("/")
        check(re.match(r"\S+ \S+ \S+ / / ro[ ,]", root), f"/ is not mounted read-only: {root}")
        rc, _ = vm.run("touch /cherry-rw-test", check_rc=False)
        check(rc != 0, "the root filesystem is writable")
        var = vm.fstype("/var")
        check(" - tmpfs " in var, f"/var is not a tmpfs: {var}")
        check("nosuid" not in var.split(" - ")[0], f"/var is nosuid, which breaks setuid in containers: {var}")
        check(vm.run("readlink -f /root") == "/var/roothome", "/root is not on /var")
        check("NAME=Cherry" in vm.run("cat /etc/os-release"), "unexpected os-release")
        check("console=ttyS0" in vm.run("cat /proc/cmdline"), "the UKI command line was not used")
        check(len(vm.run("cat /etc/machine-id")) == 32, "no machine-id")
        failed = vm.run("systemctl --failed --no-legend --plain")
        check(failed == "", f"failed units:\n{failed}")
        check(vm.run("systemctl is-active sshd.service") == "active", "sshd is not running")
        vm.run("test -s /var/lib/sshd/etc/ssh/ssh_host_ed25519_key.pub")
        check(vm.run("cat /root/.ssh/authorized_keys") == TEST_KEY,
              "the ssh.authorized_keys.root credential was not applied")
        vm.run("for i in $(seq 60); do ip -4 addr show | grep -q 'inet 10\\.0\\.2\\.' && exit 0; sleep 1; done; "
               "networkctl; exit 1", timeout=90)
        # systemd arms it before journald runs, so its message is only in the kernel log.
        vm.run("test -c /dev/watchdog0 && dmesg | grep -q 'Using hardware watchdog'")
        check(vm.run("systemctl show -p RuntimeWatchdogUSec --value") == "30s", "RuntimeWatchdogSec not applied")
        self.machine_id = vm.run("cat /etc/machine-id")
        log("memory of the booted OS, before any container:\n" + vm.run(
            "free -k; df -k /var /run | sed 's/^/  /'; "
            "grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|Shmem|Slab|KernelStack|PageTables):' /proc/meminfo"))

    def s2_nspawn(self):
        vm = self.vm
        root = "/var/lib/machines/smoke"
        vm.run(f"rm -rf {root} && mkdir -p {root}/usr {root}/etc && "
               f"cp /usr/lib/os-release {root}/etc/os-release && "
               f"for l in bin sbin lib lib64; do [ -L /$l ] && ln -s $(readlink /$l) {root}/$l; done; ls -l {root}")
        vm.run(f"systemd-run --unit=smoke-nspawn systemd-nspawn -M smoke -D {root} --bind-ro=/usr "
               "--network-veth /bin/sh -c 'ip link set host0 up && exec sleep 100000'")
        vm.run("for i in $(seq 60); do machinectl show smoke -p State --value 2>/dev/null | grep -qx running "
               "&& exit 0; sleep 1; done; journalctl -u smoke-nspawn --no-pager; exit 1", timeout=120)
        vm.run("for i in $(seq 60); do networkctl status ve-smoke | grep -Eq 'State: .*\\(configured' "
               "&& exit 0; sleep 1; done; networkctl status ve-smoke; exit 1", timeout=120)
        vm.run("nft list table ip io.systemd.nat")
        vm.run("machinectl terminate smoke; for i in $(seq 30); do machinectl show smoke >/dev/null 2>&1 || exit 0; "
               "sleep 1; done; exit 1", timeout=60)

    def s3_stateless_reboot(self):
        vm = self.vm
        vm.run("touch /var/cherry-smoke")
        self.boot()
        rc, _ = vm.run("test -e /var/cherry-smoke", check_rc=False)
        check(rc != 0, "/var survived a reboot")
        check(vm.run("cat /etc/machine-id") != self.machine_id, "the transient machine-id did not change")

    def s4_debootstrap(self):
        vm = self.vm
        mirror = "http://deb.debian.org/debian"
        # systemd-resolved can fail lookups for a while after boot (DNSSEC, DoT probing), so retry.
        rc, out = vm.run(f"for i in $(seq 12); do wget -q -T 10 -t 1 -O /dev/null {mirror}/dists/trixie/InRelease "
                         "&& exit 0; sleep 5; done; resolvectl query deb.debian.org; exit 1", timeout=240, check_rc=False)
        if rc != 0:
            log(f"skipped: the guest cannot reach {mirror}: {out}")
            return
        root = "/var/lib/machines/debian"
        vm.run(f"debootstrap --variant=minbase --include=systemd,systemd-sysv,dbus trixie {root} {mirror} "
               ">/tmp/debootstrap.log 2>&1 || { tail -n 40 /tmp/debootstrap.log; exit 1; }", timeout=1800)
        check("VERSION_CODENAME=trixie" in vm.run(f"cat {root}/etc/os-release"), "debootstrap did not install trixie")
        vm.run("machinectl start debian")
        # The container's console goes to the host journal. Wait there rather than with
        # `systemctl -M debian is-system-running --wait`, which can block forever when started this early.
        vm.run("for i in $(seq 120); do journalctl -u systemd-nspawn@debian --no-pager "
               "| grep -q 'Reached target multi-user.target' && exit 0; sleep 1; done; "
               "journalctl -u systemd-nspawn@debian --no-pager | tail -n 40; exit 1", timeout=300)
        rc, state = vm.run("systemctl -M debian is-system-running", check_rc=False)
        check(state in ("running", "degraded"), f"the Debian container is {state!r}")
        if state != "running":
            log("the Debian container is degraded:\n" + vm.run("systemctl -M debian --failed --no-legend --plain"))
        version = vm.run("systemd-run -M debian --wait -q -P cat /etc/debian_version")
        check(version.startswith("13"), f"unexpected /etc/debian_version in the container: {version!r}")
        vm.run("machinectl terminate debian; for i in $(seq 30); do machinectl show debian >/dev/null 2>&1 || exit 0; "
               "sleep 1; done; exit 1", timeout=60)
        vm.run(f"rm -rf {root}")

    def run(self):
        scenarios = [self.s1_http_boot, self.s2_nspawn, self.s3_stateless_reboot, self.s4_debootstrap]
        try:
            for i, scenario in enumerate(scenarios, 1):
                name = scenario.__name__[len(f"s{i}_"):].replace("_", " ")
                log(f"=== {i}. {name}")
                start = time.monotonic()
                scenario()
                log(f"=== {i}. passed in {time.monotonic() - start:.0f}s")
        finally:
            self.vm.stop()
            self.server.shutdown()
        log(f"all scenarios passed (serial log: {os.path.join(self.workdir, 'serial.log')})")


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
