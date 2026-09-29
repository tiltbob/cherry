#!/usr/bin/env python3
"""End-to-end smoke test for a Cherry image in QEMU + OVMF (Python stdlib only).

All scenarios share one VM state (disk overlay + UEFI variable store) and run
in order, each building on the previous one:

  1. first boot with empty NVRAM: removable-media fallback into slot A, boot
     entries created, slot promoted, /var grown, /etc overlay, pairing checks
  2. systemd-nspawn container with a veth link and networkd masquerading
  3. update A -> B from a local HTTP server (streaming), trial boot via
     BootNext, promotion, /etc + machine-id + SSH host key persistence
  4. bundles signed with an untrusted key, or in plain format, are rejected
  5. rollback when the updated slot panics
  6. rollback when the updated slot boots but never becomes healthy
  7. repair of a deleted boot entry
  8. recovery from NVRAM loss
  9. kernel/rootfs skew (fallback boots the other slot's ESP) is redirected
 10. an injected root= that disagrees with rauc.slot= is never promoted

Timeouts scale with CHERRY_TEST_TIMEOUT_MULT (default 1 with KVM, 4 without).
"""

import argparse
import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import run_qemu  # noqa: E402

MULT = float(os.environ.get("CHERRY_TEST_TIMEOUT_MULT", "1" if run_qemu.kvm_usable() else "4"))

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|[=>78cDEHM])")
BOOT = re.compile(r'BdsDxe: starting Boot([0-9A-F]{4}) "([^"]*)"')
PANIC = re.compile(r"Kernel panic - not syncing")
FATAL = [
    (re.compile(r"No bootable option"), "the firmware found nothing to boot"),
    (re.compile(r'BdsDxe: starting Boot[0-9A-F]{4} "(?:EFI Internal Shell|UEFI PXE|UEFI HTTP)'),
     "the firmware fell through to the UEFI shell or network boot"),
    (re.compile(r"^Shell> ", re.M), "the firmware started the UEFI shell"),
    (re.compile(r"Found ordering cycle"), "systemd found an ordering cycle"),
]
FALLBACK_LABEL = re.compile(r"UEFI Misc Device")


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

    def _check_fatal(self, text, allow_boot, allow_panic):
        for pattern, reason in FATAL:
            if pattern.search(text):
                raise TestFailure(f"{reason}:\n{text[-3000:]}")
        if not allow_boot and BOOT.search(text):
            raise TestFailure(f"unexpected reboot:\n{text[-3000:]}")
        if not allow_panic and PANIC.search(text):
            raise TestFailure(f"unexpected kernel panic:\n{text[-3000:]}")

    def expect(self, pattern, timeout, allow_boot=False, allow_panic=False):
        """Wait for pattern; return (match, text before the match)."""
        if isinstance(pattern, str):
            pattern = re.compile(pattern, re.M)
        deadline = time.monotonic() + timeout * MULT
        with self.cond:
            while True:
                window = self.buf[self.pos:]
                m = pattern.search(window)
                self._check_fatal(window[: m.end()] if m else window, allow_boot, allow_panic)
                if m:
                    self.pos += m.end()
                    return m, window[: m.start()]
                if self.eof:
                    raise TestFailure(f"QEMU exited while waiting for {pattern.pattern!r}:\n{window[-3000:]}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Timeout(f"timed out waiting for {pattern.pattern!r}; last output:\n{window[-3000:]}")
                self.cond.wait(min(remaining, 1.0))

    def quiet_for(self, seconds, allow_boot=False):
        """Fail if a boot (or anything fatal) shows up in the next `seconds`."""
        try:
            self.expect(BOOT, seconds / MULT, allow_boot=True)
        except Timeout:
            return
        if not allow_boot:
            raise TestFailure("unexpected reboot while waiting")


class EFI:
    """Parsed `efibootmgr -v` output."""

    def __init__(self, text):
        self.entries = {}
        for line in text.splitlines():
            m = re.match(r"Boot([0-9A-Fa-f]{4})[* ] ([^\t]*)\t?(.*)$", line)
            if m:
                self.entries[m[1].upper()] = (m[2], m[3])
        m = re.search(r"^BootOrder: (\S+)$", text, re.M)
        self.order = m[1].upper().split(",") if m else []
        m = re.search(r"^BootNext: ([0-9A-Fa-f]{4})$", text, re.M)
        self.next = m[1].upper() if m else None
        m = re.search(r"^BootCurrent: ([0-9A-Fa-f]{4})$", text, re.M)
        self.current = m[1].upper() if m else None

    def nums(self, label):
        return [n for n, (lbl, _) in self.entries.items() if lbl == label]

    def one(self, label):
        nums = self.nums(label)
        check(len(nums) == 1, f"expected exactly one {label} entry, got {nums}: {self.entries}")
        return nums[0]


class VM:
    def __init__(self, state, workdir):
        self.state = state
        self.workdir = workdir
        self.proc = None
        self.console = None
        self.n = 0

    def start(self, smbios=()):
        self.stop()
        cmd = run_qemu.qemu_command(self.state, serial="stdio", credentials=["agetty.autologin=root"],
                                    smbios_strings=smbios)
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

    def wait_boot(self, label, timeout=180, allow_panic=False):
        """Wait until the firmware starts a boot entry; check its label."""
        m, _ = self.console.expect(BOOT, timeout, allow_boot=True, allow_panic=allow_panic)
        actual = m[2]
        ok = FALLBACK_LABEL.search(actual) if label == "fallback" else actual == label
        check(ok, f"firmware started {actual!r} (Boot{m[1]}), expected {label!r}")
        log(f"firmware started Boot{m[1]} {actual!r}")
        return m[1]

    def wait_shell(self, timeout=420):
        start = time.monotonic()
        m, _ = self.console.expect(r"login: root \(automatic login\)|login: $", timeout)
        if "automatic" not in m[0]:
            self.send("root\n")
        for _ in range(60):
            self.send("echo @@READY@@\n")
            try:
                self.console.expect(r"^@@READY@@$", 5)
                break
            except Timeout:
                continue
        else:
            raise TestFailure("no shell on the serial console")
        self.send("stty -echo; dmesg -n 1\n")
        self.run("true")
        return time.monotonic() - start

    def run(self, cmd, timeout=120, check_rc=True):
        self.n += 1
        n = self.n
        self.send(f"echo @@B{n}@@; ( {cmd} ) 2>&1; echo @@E{n}:$?@@\n")
        self.console.expect(rf"^@@B{n}@@$", timeout)
        m, output = self.console.expect(rf"^@@E{n}:(\d+)@@$", timeout)
        rc = int(m[1])
        output = output.strip("\n")
        if check_rc and rc != 0:
            raise TestFailure(f"`{cmd}` exited with {rc}:\n{output}")
        return output if check_rc else (rc, output)

    def efi(self):
        return EFI(self.run("efibootmgr -v"))

    def rauc_status(self):
        return json.loads(self.run("rauc status --output-format=json"))

    def settle(self):
        """Wait for boot to finish; return systemd's system state."""
        rc, state = self.run("systemctl is-system-running --wait", timeout=600, check_rc=False)
        state = state.splitlines()[-1] if state else ""
        if state != "running":
            failed = self.run("systemctl --failed --no-legend --plain; journalctl -b -p warning --no-pager | tail -n 60",
                              check_rc=False)[1]
            log(f"system state {state!r}:\n{failed}")
        return state

    def reboot(self):
        self.send("systemctl reboot\n")


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Static files with single-range support; RAUC streaming needs HTTP 206."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_HEAD(self):
        self._serve(False)

    def do_GET(self):
        self._serve(True)

    def _serve(self, body):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
            if not m or not (m[1] or m[2]):
                self.send_error(416)
                return
            if m[1]:
                start = int(m[1])
                end = min(int(m[2]), size - 1) if m[2] else size - 1
            else:
                start = max(0, size - int(m[2]))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not body:
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def serve(directory):
    handler = functools.partial(RangeHandler, directory=directory)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def make_bad_bundles(host_dir, workdir, cert, key, keyring):
    """A bundle signed by an untrusted key, and a correctly signed plain one."""
    env = dict(os.environ, PATH=os.path.join(host_dir, "bin") + os.pathsep + os.environ["PATH"])
    rauc = os.path.join(host_dir, "bin", "rauc")
    out = os.path.join(workdir, "www")
    os.makedirs(out, exist_ok=True)
    manifest = "[update]\ncompatible=cherry-x86_64\nversion=smoke\n\n{bundle}[image.rootfs]\nfilename=rootfs.img\n"

    def bundle(name, bundle_section, cert, key, keyring=None):
        stage = os.path.join(workdir, f"stage-{name}")
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage)
        with open(os.path.join(stage, "rootfs.img"), "wb") as f:
            f.write(b"\0" * 65536)
        with open(os.path.join(stage, "manifest.raucm"), "w") as f:
            f.write(manifest.format(bundle=bundle_section))
        target = os.path.join(out, f"{name}.raucb")
        if os.path.exists(target):
            os.unlink(target)
        cmd = [rauc, "bundle", f"--cert={cert}", f"--key={key}"]
        if keyring:
            cmd.append(f"--signing-keyring={keyring}")
        subprocess.run(cmd + [stage, target], check=True, env=env, stdout=subprocess.DEVNULL)

    tmp_key = os.path.join(workdir, "untrusted.key.pem")
    tmp_cert = os.path.join(workdir, "untrusted.cert.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/O=Untrusted/CN=untrusted", "-keyout", tmp_key, "-out", tmp_cert],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bundle("untrusted", "[bundle]\nformat=verity\n\n", tmp_cert, tmp_key)
    bundle("plain", "", cert, key, keyring)
    return out


class Smoke:
    def __init__(self, args):
        self.args = args
        self.workdir = os.path.abspath(args.state)
        if os.path.exists(self.workdir):
            shutil.rmtree(self.workdir)
        os.makedirs(self.workdir)
        self.state = run_qemu.State(args.images, os.path.join(self.workdir, "vm"))
        self.state.prepare(args.disk_size)
        self.vm = VM(self.state, self.workdir)
        self.boot_time = 120.0

        www = make_bad_bundles(args.host_dir, self.workdir, args.cert, args.key, args.keyring)
        os.symlink(os.path.join(os.path.abspath(args.images), "cherry-x86_64.raucb"),
                   os.path.join(www, "cherry-x86_64.raucb"))
        self.http = serve(www)
        self.url = f"http://10.0.2.2:{self.http.server_address[1]}"

    # -- helpers -------------------------------------------------------------

    def booted(self, slot, promote=True):
        """Assertions for a healthy boot of `slot`; returns the efibootmgr state."""
        vm = self.vm
        state = vm.settle()
        check(state == "running", f"system is {state!r}, not running")
        cmdline = vm.run("cat /proc/cmdline")
        check(re.findall(r"(?:^| )rauc\.slot=(\S+)", cmdline) == [f"cherry-{slot}"],
              f"expected exactly one rauc.slot=cherry-{slot}: {cmdline}")
        status = vm.rauc_status()
        check(status.get("booted") == f"cherry-{slot}", f"RAUC booted slot is {status.get('booted')!r}")
        check(vm.run("systemctl is-active cherry-health.service") == "active", "cherry-health is not active")
        efi = vm.efi()
        num = efi.one(f"cherry-{slot}")
        efi.one("cherry-" + ("b" if slot == "a" else "a"))
        for label, path in efi.entries.values():
            if label.startswith("cherry-"):
                check(re.fullmatch(r"HD\([^)]*\)/File\(\\EFI\\cherry\\cherry-[ab]\.efi\)", path, re.I),
                      f"entry {label} is malformed or has load options: {path!r}")
        if promote:
            check(efi.order[:1] == [num], f"cherry-{slot} (Boot{num}) is not first in BootOrder {efi.order}")
            check(efi.next is None, f"BootNext still set: {efi.next}")
        return efi

    def install(self, bundle="cherry-x86_64.raucb"):
        return self.vm.run(f"rauc install {self.url}/{bundle}", timeout=900, check_rc=False)

    # -- scenarios -----------------------------------------------------------

    def s1_first_boot(self):
        vm = self.vm
        vm.start()
        vm.wait_boot("fallback")
        self.boot_time = vm.wait_shell()
        log(f"reached a shell {self.boot_time:.0f}s after the firmware started the kernel")
        efi = self.booted("a")
        check(f"{efi.one('cherry-b')}" not in efi.order, "cherry-b (empty slot) must not be in BootOrder")

        vm.run("test \"$(awk '$5 == \"/etc\" { t = $0 } END { print t }' /proc/self/mountinfo | "
               "sed 's/.* - //' | cut -d' ' -f1)\" = overlay")
        check(vm.run("readlink -f /var/run") == "/run", "/var/run does not resolve to /run")
        check(vm.run("readlink -f /root") == "/var/roothome", "/root is not on /var")
        check(vm.run("ls -ld /var/roothome | cut -c1-10") == "drwx------", "/var/roothome is not 0700")
        data_kib = int(vm.run("df -k /var | awk 'NR == 2 { print $2 }'"))
        check(data_kib > 1024 * 1024, f"/var was not grown ({data_kib} KiB)")
        check(vm.run("cat /sys/class/watchdog/watchdog0/state") == "active", "hardware watchdog not armed")
        check(vm.run("systemctl is-enabled systemd-boot-check-no-failures.service") == "enabled",
              "systemd-boot-check-no-failures is not enabled")
        check(vm.run("mountpoint -q /boot", check_rc=False)[0] != 0, "something is mounted at /boot")
        check(vm.run("systemctl --failed --no-legend --plain") == "", "failed units")
        check(vm.run("systemctl is-active sshd.service") == "active", "sshd is not running")

        machine_id = vm.run("cat /etc/machine-id")
        entries = {n: e for n, e in efi.entries.items() if e[0].startswith("cherry-")}
        vm.reboot()
        vm.wait_boot("cherry-a")
        vm.wait_shell()
        efi = self.booted("a")
        check(vm.run("cat /etc/machine-id") == machine_id, "machine-id changed across reboot")
        check({n: e for n, e in efi.entries.items() if e[0].startswith("cherry-")} == entries,
              "boot entries changed across a reboot")

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
        vm.run(f"rm -rf {root}")

    def s3_update(self):
        vm = self.vm
        vm.run("echo persisted > /etc/cherry-smoke")
        machine_id = vm.run("cat /etc/machine-id")
        host_key = vm.run("sha256sum /etc/ssh/ssh_host_ed25519_key.pub")
        rc, out = self.install()
        check(rc == 0, f"rauc install failed:\n{out}")
        efi = vm.efi()
        num_b = efi.one("cherry-b")
        check(efi.next == num_b, f"BootNext is {efi.next}, expected cherry-b (Boot{num_b})")
        check(num_b not in efi.order, "cherry-b must stay out of BootOrder until it is confirmed")
        vm.reboot()
        vm.wait_boot("cherry-b")
        vm.wait_shell()
        efi = self.booted("b")
        check(efi.order[:2] == [efi.one("cherry-b"), efi.one("cherry-a")],
              f"BootOrder should start cherry-b,cherry-a: {efi.order}")
        check(vm.run("cat /etc/cherry-smoke") == "persisted", "/etc changes were lost across the update")
        check(vm.run("cat /etc/machine-id") == machine_id, "machine-id changed across the update")
        check(vm.run("sha256sum /etc/ssh/ssh_host_ed25519_key.pub") == host_key, "SSH host key changed")

    def s4_rejected_bundles(self):
        vm = self.vm
        before = vm.efi()
        for bundle in ("untrusted.raucb", "plain.raucb"):
            rc, out = self.install(bundle)
            check(rc != 0, f"{bundle} was installed:\n{out}")
            log(f"{bundle} rejected: {out.splitlines()[-1] if out else ''}")
        after = vm.efi()
        check((after.order, after.next) == (before.order, before.next), "a rejected install changed BootOrder/BootNext")

    def s5_panic_rollback(self):
        vm = self.vm
        rc, out = self.install()
        check(rc == 0, f"rauc install failed:\n{out}")
        vm.run("dd if=/dev/zero of=/dev/disk/by-partlabel/cherry-root-a bs=1M count=1 conv=fsync")
        vm.reboot()
        vm.wait_boot("cherry-a")
        self.vm.console.expect(PANIC, 300, allow_panic=True)
        log("slot A panicked as expected")
        vm.wait_boot("cherry-b", timeout=180, allow_panic=True)
        vm.wait_shell()
        efi = self.booted("b")
        check(efi.one("cherry-a") not in efi.order, "the failed slot A is still in BootOrder")
        slots = {k: v for s in vm.rauc_status()["slots"] for k, v in s.items()}
        check(slots["rootfs.0"].get("boot_status") == "bad", f"rootfs.0 is not bad: {slots['rootfs.0']}")

    def s6_unhealthy_rollback(self):
        vm = self.vm
        deadline = int(max(120, 2 * self.boot_time + 60))
        vm.run("printf '%s\\n' '[Unit]' 'ConditionKernelCommandLine=rauc.slot=cherry-a' 'Before=boot-complete.target' "
               "'[Service]' 'Type=oneshot' 'ExecStart=/bin/false' '[Install]' 'RequiredBy=boot-complete.target' "
               "> /etc/systemd/system/cherry-test-fail.service")
        vm.run("mkdir -p /etc/systemd/system/cherry-trial-deadline.timer.d && "
               f"printf '[Timer]\\nOnBootSec=\\nOnBootSec={deadline}s\\n' "
               "> /etc/systemd/system/cherry-trial-deadline.timer.d/smoke.conf")
        vm.run("systemctl daemon-reload && systemctl enable cherry-test-fail.service")
        rc, out = self.install()
        check(rc == 0, f"rauc install failed:\n{out}")
        vm.reboot()
        vm.wait_boot("cherry-a")
        vm.wait_shell()
        vm.settle()
        check(vm.run("systemctl is-failed cherry-test-fail.service", check_rc=False)[1] == "failed",
              "the injected failure did not happen")
        efi = vm.efi()
        check(efi.one("cherry-a") not in efi.order, "the unhealthy slot A was promoted")
        log(f"waiting up to {deadline}s for the trial deadline")
        vm.wait_boot("cherry-b", timeout=deadline + 300)
        vm.wait_shell()
        self.booted("b")
        vm.run("journalctl -b -1 -u cherry-test-fail.service --no-pager | grep -q .")
        log("confirmed slot B stays up past its own deadline")
        vm.console.quiet_for(deadline * 1.2)
        vm.run("systemctl disable cherry-test-fail.service && rm -rf /etc/systemd/system/cherry-test-fail.service "
               "/etc/systemd/system/cherry-trial-deadline.timer.d && systemctl daemon-reload")

    def s7_entry_repair(self):
        vm = self.vm
        efi = vm.efi()
        booted = efi.one("cherry-b")
        vm.run(f"efibootmgr -q -b {efi.one('cherry-a')} -B && systemctl restart cherry-efi-entries.service")
        efi = vm.efi()
        num_a = efi.one("cherry-a")
        check(num_a not in efi.order, "the re-created cherry-a entry must not be in BootOrder")
        check(efi.one("cherry-b") == booted and efi.order[0] == booted, "the booted entry was touched")

    def s8_nvram_loss(self):
        vm = self.vm
        vm.stop()
        self.state.reset_vars()
        vm.start()
        vm.wait_boot("fallback")
        vm.wait_shell()
        self.booted("a")
        check(vm.run("systemctl is-active rauc.service") == "active", "rauc.service is not running")

    def s9_skew(self):
        vm = self.vm
        vm.run("mount -t vfat /dev/disk/by-partlabel/cherry-efi-a /mnt && rm /mnt/EFI/BOOT/BOOTX64.EFI; "
               "rc=$?; umount /mnt; exit $rc")
        vm.stop()
        self.state.reset_vars()
        vm.start()
        vm.wait_boot("fallback")
        vm.wait_boot("cherry-b", timeout=600)
        vm.wait_shell()
        self.booted("b")
        vm.run("test ! -e /var/lib/cherry/redirected")
        vm.run("journalctl -b -1 -u cherry-health.service --no-pager | grep -q 'rebooting into cherry-b'")

    def s10_injected_root(self):
        vm = self.vm
        vm.stop()
        vm.start(smbios=["io.systemd.stub.kernel-cmdline-extra=root=PARTLABEL=cherry-root-a"])
        vm.wait_boot("cherry-b")
        vm.wait_shell()
        vm.settle()
        check(vm.run("sed -n 's/^PARTNAME=//p' /sys/dev/block/$(mountpoint -d /)/uevent") == "cherry-root-a",
              "the injected root= was not used; the test is ineffective")
        check(vm.run("systemctl is-failed cherry-health.service", check_rc=False)[1] == "failed",
              "cherry-health accepted a root filesystem from the wrong slot")
        efi = vm.efi()
        check(efi.order[0] == efi.one("cherry-b") and efi.next is None, "BootOrder changed")
        rc, out = self.install()
        check(rc != 0, "rauc install was allowed from an unhealthy boot")
        vm.stop()
        vm.start()
        vm.wait_boot("cherry-b")
        vm.wait_shell()
        self.booted("b")

    def run(self):
        scenarios = [
            self.s1_first_boot, self.s2_nspawn, self.s3_update, self.s4_rejected_bundles, self.s5_panic_rollback,
            self.s6_unhealthy_rollback, self.s7_entry_repair, self.s8_nvram_loss, self.s9_skew,
            self.s10_injected_root,
        ]
        selected = set(self.args.only.split(",")) if self.args.only else None
        try:
            for i, scenario in enumerate(scenarios, 1):
                if selected and str(i) not in selected:
                    continue
                log(f"=== {i}. {scenario.__name__[len(f's{i}_'):].replace('_', ' ')}")
                start = time.monotonic()
                scenario()
                log(f"=== {i}. passed in {time.monotonic() - start:.0f}s")
        finally:
            self.vm.stop()
            self.http.shutdown()
        log(f"all scenarios passed (serial log: {os.path.join(self.workdir, 'serial.log')})")


def main():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images", default=os.path.join(root, "output/cherry_x86_64/images"))
    parser.add_argument("--host-dir", help="Buildroot host directory (default: <images>/../host)")
    parser.add_argument("--state", default=os.path.join(root, "output/cherry_x86_64/test"))
    parser.add_argument("--disk-size", default="4G")
    parser.add_argument("--cert", default=os.environ.get("CHERRY_RAUC_CERT", os.path.join(root, "keys/dev.cert.pem")))
    parser.add_argument("--key", default=os.environ.get("CHERRY_RAUC_KEY", os.path.join(root, "keys/dev.key.pem")))
    parser.add_argument("--keyring", default=os.environ.get("CHERRY_RAUC_KEYRING"))
    parser.add_argument("--only", help="comma-separated scenario numbers (they build on each other)")
    args = parser.parse_args()
    args.host_dir = args.host_dir or os.path.join(os.path.abspath(args.images), "..", "host")
    args.keyring = args.keyring or args.cert
    log(f"timeout multiplier {MULT:g} ({'KVM' if run_qemu.kvm_usable() else 'TCG'})")
    try:
        Smoke(args).run()
    except TestFailure as e:
        log(f"FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
