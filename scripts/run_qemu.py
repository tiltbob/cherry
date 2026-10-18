#!/usr/bin/env python3
"""UEFI HTTP boot of Cherry in QEMU (q35 + OVMF).

Serves the images directory over HTTP on the host, writes a fresh OVMF
variable store whose BootNext is an HTTP boot entry for the EFI binary
(http://10.0.2.2:<port>/cherry-x86_64.efi, as seen from QEMU's user-mode
network), starts a software TPM 2.0 (swtpm), and starts QEMU with the serial
console on this terminal.

The swtpm state in <state>/tpm is the VM's TPM, and so its identity: it stays
the same machine across runs until that directory is deleted. Without a disk,
every run otherwise starts from scratch. --disk SIZE attaches <state>/disk.img,
a sparse virtio disk created blank with that size, for the agent to give to
the VM's pool cherry, which puts /var on it, its key sealed by that TPM, and
unlocks it at the next boot. Delete both for a new machine. --hdd SIZE and
--ssd SIZE, each repeatable, attach more disks for storage pools, blank and
kept likewise:
<state>/hdd<N>.img on virtio-blk, which the guest sees as hard disks, and
<state>/ssd<N>.img on NVMe, which it sees as SSDs.

With --secure-boot, the firmware is OVMF's Secure Boot build (with SMM), and
its variable store, <state>/OVMF_VARS.secboot.fd, persists too. A new one is
in setup mode, so Cherry enrolls its keys and reboots; QEMU then exits, as the
HTTP boot entry is for one boot only (BootNext): run it again to boot on, now
with Secure Boot. An unsigned image no longer boots then. Delete the file for
a machine in setup mode again.

Needs qemu-system-x86_64, OVMF, swtpm and virt-fw-vars (python3-virt-firmware,
or `pip install virt-firmware`; set VIRT_FW_VARS to use a specific copy).

Never add pvpanic: QEMU would exit on a kernel panic instead of resetting.

Also used as a module by tests/smoke.py.
"""

import argparse
import functools
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

EFI_NAME = "cherry-x86_64.efi"
TPM_INTERFACES = ("crb", "tis")

OVMF_CANDIDATES = [
    # Debian/Ubuntu
    ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
    ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
    # Fedora
    ("/usr/share/edk2/ovmf/OVMF_CODE.fd", "/usr/share/edk2/ovmf/OVMF_VARS.fd"),
    # Arch
    ("/usr/share/edk2/x64/OVMF_CODE.4m.fd", "/usr/share/edk2/x64/OVMF_VARS.4m.fd"),
]
# The Secure Boot builds, which need SMM, and their empty variable stores: setup mode.
OVMF_SECBOOT_CANDIDATES = [
    ("/usr/share/OVMF/OVMF_CODE_4M.secboot.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
    ("/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd", "/usr/share/edk2/ovmf/OVMF_VARS.fd"),
    ("/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd", "/usr/share/edk2/x64/OVMF_VARS.4m.fd"),
]
SECBOOT_VARS = "OVMF_VARS.secboot.fd"


def find_ovmf(secure_boot=False):
    env = "OVMF_SECBOOT" if secure_boot else "OVMF"
    code, vars_ = os.environ.get(f"{env}_CODE"), os.environ.get(f"{env}_VARS")
    if code and vars_:
        return code, vars_
    for code, vars_ in OVMF_SECBOOT_CANDIDATES if secure_boot else OVMF_CANDIDATES:
        if os.path.exists(code) and os.path.exists(vars_):
            return code, vars_
    sys.exit(f"OVMF{' with Secure Boot' if secure_boot else ''} not found: install it (e.g. apt install ovmf) "
             f"or set {env}_CODE and {env}_VARS")


def kvm_usable():
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def parse_size(text):
    """A size in bytes from e.g. 8G, 512M or 1073741824 (K, M, G and T are powers of 1024)."""
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    text = text.upper()
    if text[-1:] in units:
        return int(text[:-1]) * units[text[-1]]
    return int(text)


def make_disk(path, size):
    """A blank (all zeros) sparse raw disk image of `size` bytes at `path`, unless one is there already."""
    try:
        with open(path, "xb") as f:
            f.truncate(size)
    except FileExistsError:
        pass


class ImageHandler(http.server.SimpleHTTPRequestHandler):
    """Static files; EFI binaries are served as application/efi."""

    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".efi": "application/efi"}
    requests = []

    def log_message(self, fmt, *args):
        ImageHandler.requests.append(self.path)


def serve(directory, port=0):
    """Serve `directory` on 127.0.0.1 (10.0.2.2 inside the VM)."""
    handler = functools.partial(ImageHandler, directory=directory)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def boot_url(server, name=EFI_NAME):
    return f"http://10.0.2.2:{server.server_address[1]}/{name}"


def virt_fw_vars():
    tool = os.environ.get("VIRT_FW_VARS") or shutil.which("virt-fw-vars")
    if not tool:
        sys.exit("virt-fw-vars not found: apt install python3-virt-firmware, or pip install virt-firmware")
    return tool


def write_vars(path, url, secure_boot=False):
    """Fresh UEFI variable store that HTTP boots `url` once (BootNext); for Secure Boot, in setup mode."""
    subprocess.run([virt_fw_vars(), "--input", find_ovmf(secure_boot)[1], "--output", path, "--set-boot-uri", url],
                   check=True, stdout=subprocess.DEVNULL)


def set_boot_uri(path, url):
    """HTTP boot `url` once (BootNext) from an existing variable store, keeping the rest."""
    subprocess.run([virt_fw_vars(), "--inplace", path, "--set-boot-uri", url], check=True, stdout=subprocess.DEVNULL)


class Swtpm:
    """A software TPM 2.0 (swtpm) for one QEMU run, with its state in state_dir.

    The same state directory gives the same endorsement key, so the same machine
    identity. swtpm exits when QEMU disconnects.
    """

    def __init__(self, state_dir):
        tool = shutil.which("swtpm")
        if not tool:
            sys.exit("swtpm not found: apt install swtpm, or boot without a TPM (--tpm none)")
        os.makedirs(state_dir, exist_ok=True)
        # Short path: Unix socket paths are limited to 108 bytes.
        self.sock_dir = tempfile.mkdtemp(prefix="cherry-swtpm-")
        self.socket = os.path.join(self.sock_dir, "swtpm.sock")
        self.proc = subprocess.Popen([tool, "socket", "--tpm2", "--terminate",
                                      "--tpmstate", f"dir={state_dir}",
                                      "--ctrl", f"type=unixio,path={self.socket}",
                                      "--log", f"file={os.path.join(state_dir, 'swtpm.log')}"])
        deadline = time.monotonic() + 10
        while not os.path.exists(self.socket):
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.stop()
                sys.exit(f"swtpm did not start; see {os.path.join(state_dir, 'swtpm.log')}")
            time.sleep(0.05)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        shutil.rmtree(self.sock_dir, ignore_errors=True)


def qemu_command(vars_path, serial="mon:stdio", ssh_port=None, credentials=(), memory="3072", cpus="2",
                 tpm=None, tpm_interface="crb", secure_boot=False, disks=(), ssds=()):
    """With secure_boot, OVMF's Secure Boot build, whose variables only SMM may write; and QEMU exits when the
    guest reboots, as the firmware would not HTTP boot again. disks are raw images on virtio-blk, which the guest
    sees as rotational, hard disks; ssds raw images on NVMe, which it sees as SSDs."""
    code, _ = find_ovmf(secure_boot)
    cmd = ["qemu-system-x86_64", "-machine", "q35,smm=on" if secure_boot else "q35", "-m", str(memory),
           "-smp", str(cpus)]
    if secure_boot:
        cmd += ["-global", "driver=cfi.pflash01,property=secure,value=on",
                "-global", "ICH9-LPC.disable_s3=1", "-action", "reboot=shutdown"]
    if kvm_usable():
        cmd += ["-accel", "kvm", "-cpu", "host"]
    else:
        cmd += ["-accel", "tcg,thread=multi", "-cpu", "max"]
    cmd += [
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,unit=1,file={vars_path}",
        "-device", "virtio-rng-pci",
        "-device", "i6300esb",
        "-action", "watchdog=reset",
        "-display", "none",
        "-serial", serial,
    ]
    if not serial.startswith("mon:"):
        cmd += ["-monitor", "none"]
    netdev = "user,model=virtio-net-pci"
    if ssh_port:
        netdev += f",hostfwd=tcp:127.0.0.1:{ssh_port}-:22"
    cmd += ["-nic", netdev]
    if tpm:
        cmd += ["-chardev", f"socket,id=chrtpm,path={tpm.socket}",
                "-tpmdev", "emulator,id=tpm0,chardev=chrtpm",
                "-device", f"tpm-{tpm_interface},tpmdev=tpm0"]
    # Raw disk images on virtio-blk, vda, vdb, ... in this order. Discards punch holes in sparse images.
    for path in disks:
        cmd += ["-drive", f"if=virtio,format=raw,discard=unmap,file={path}"]
    # NVMe controllers, nvme0n1, nvme1n1, ... in this order, each with a serial of its own.
    for i, path in enumerate(ssds):
        cmd += ["-drive", f"if=none,id=ssd{i},format=raw,discard=unmap,file={path}",
                "-device", f"nvme,drive=ssd{i},serial=cherry-ssd{i}"]
    # systemd credentials via SMBIOS type 11 strings.
    for key_value in credentials:
        cmd += ["-smbios", f"type=11,value=io.systemd.credential:{key_value}"]
    return cmd


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images", default="output/cherry_x86_64/images")
    parser.add_argument("--state", default="output/cherry_x86_64/qemu",
                        help="directory for the variable store and the TPM state")
    parser.add_argument("--port", type=int, default=0, help="HTTP server port (default: any free port)")
    parser.add_argument("--ssh-port", type=int, default=2222, help="host port forwarded to the guest's SSH")
    parser.add_argument("--credential", action="append", default=[], metavar="KEY=VALUE",
                        help="pass a systemd credential via SMBIOS, e.g. "
                             "ssh.authorized_keys.root=\"$(cat ~/.ssh/id_ed25519.pub)\" (repeatable)")
    parser.add_argument("--tpm", choices=TPM_INTERFACES + ("none",), default="crb",
                        help="TPM interface, or none to see Cherry stop without a TPM (default: crb)")
    parser.add_argument("--disk", type=parse_size, metavar="SIZE",
                        help="attach <state>/disk.img, created blank with this size (e.g. 8G) if it is not there, "
                             "for the agent to give to the pool cherry, and so /var (default: no disk)")
    parser.add_argument("--hdd", type=parse_size, action="append", default=[], metavar="SIZE",
                        help="attach <state>/hdd<N>.img, a virtio disk, created blank with this size if it is not "
                             "there, for storage pools (repeatable)")
    parser.add_argument("--ssd", type=parse_size, action="append", default=[], metavar="SIZE",
                        help="attach <state>/ssd<N>.img, an NVMe disk, likewise (repeatable)")
    parser.add_argument("--secure-boot", action="store_true",
                        help=f"OVMF with Secure Boot, its variables kept in <state>/{SECBOOT_VARS} (new: setup mode)")
    args = parser.parse_args()

    images = os.path.abspath(args.images)
    if not os.path.exists(os.path.join(images, EFI_NAME)):
        sys.exit(f"{os.path.join(images, EFI_NAME)} not found; build first")
    os.makedirs(args.state, exist_ok=True)

    server = serve(images, args.port)
    url = boot_url(server)
    if args.secure_boot:
        vars_path = os.path.join(args.state, SECBOOT_VARS)
        if os.path.exists(vars_path):
            set_boot_uri(vars_path, url)
        else:
            write_vars(vars_path, url, secure_boot=True)
            print(f"{vars_path}: a new variable store, in setup mode", file=sys.stderr)
    else:
        vars_path = os.path.join(args.state, "OVMF_VARS.fd")
        write_vars(vars_path, url)
    disks = []
    if args.disk:
        disks.append(os.path.join(args.state, "disk.img"))
        make_disk(disks[0], args.disk)
    for i, size in enumerate(args.hdd):
        disks.append(os.path.join(args.state, f"hdd{i}.img"))
        make_disk(disks[-1], size)
    ssds = []
    for i, size in enumerate(args.ssd):
        ssds.append(os.path.join(args.state, f"ssd{i}.img"))
        make_disk(ssds[-1], size)
    tpm = Swtpm(os.path.join(args.state, "tpm")) if args.tpm != "none" else None
    cmd = qemu_command(vars_path, ssh_port=args.ssh_port, credentials=args.credential,
                       tpm=tpm, tpm_interface=args.tpm, secure_boot=args.secure_boot, disks=disks, ssds=ssds)
    print(f"HTTP boot from {url}", file=sys.stderr)
    if args.secure_boot:
        print("Secure Boot: QEMU exits when the guest reboots; run this again to boot on.", file=sys.stderr)
    print("Serial console on this terminal; Ctrl-A x quits, Ctrl-A c toggles the QEMU monitor.", file=sys.stderr)
    print(" ".join(cmd), file=sys.stderr)
    try:
        sys.exit(subprocess.call(cmd))
    finally:
        if tpm:
            tpm.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
