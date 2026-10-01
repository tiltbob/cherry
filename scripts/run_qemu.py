#!/usr/bin/env python3
"""UEFI HTTP boot of Cherry in QEMU (q35 + OVMF).

Serves the images directory over HTTP on the host, writes a fresh OVMF
variable store whose BootNext is an HTTP boot entry for the EFI binary
(http://10.0.2.2:<port>/cherry-x86_64.efi, as seen from QEMU's user-mode
network), starts a software TPM 2.0 (swtpm), and starts QEMU with the serial
console on this terminal.

The swtpm state in <state>/tpm is the VM's TPM, and so its identity: it stays
the same machine across runs until that directory is deleted. Cherry keeps no
other state yet, so every run otherwise starts from scratch.

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


def find_ovmf():
    code, vars_ = os.environ.get("OVMF_CODE"), os.environ.get("OVMF_VARS")
    if code and vars_:
        return code, vars_
    for code, vars_ in OVMF_CANDIDATES:
        if os.path.exists(code) and os.path.exists(vars_):
            return code, vars_
    sys.exit("OVMF not found: install it (e.g. apt install ovmf) or set OVMF_CODE and OVMF_VARS")


def kvm_usable():
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


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


def write_vars(path, url):
    """Fresh UEFI variable store that HTTP boots `url` once (BootNext)."""
    tool = os.environ.get("VIRT_FW_VARS") or shutil.which("virt-fw-vars")
    if not tool:
        sys.exit("virt-fw-vars not found: apt install python3-virt-firmware, or pip install virt-firmware")
    subprocess.run([tool, "--input", find_ovmf()[1], "--output", path, "--set-boot-uri", url],
                   check=True, stdout=subprocess.DEVNULL)


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
                 tpm=None, tpm_interface="crb"):
    code, _ = find_ovmf()
    cmd = ["qemu-system-x86_64", "-machine", "q35", "-m", str(memory), "-smp", str(cpus)]
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
    args = parser.parse_args()

    images = os.path.abspath(args.images)
    if not os.path.exists(os.path.join(images, EFI_NAME)):
        sys.exit(f"{os.path.join(images, EFI_NAME)} not found; build first")
    os.makedirs(args.state, exist_ok=True)
    vars_path = os.path.join(args.state, "OVMF_VARS.fd")

    server = serve(images, args.port)
    url = boot_url(server)
    write_vars(vars_path, url)
    tpm = Swtpm(os.path.join(args.state, "tpm")) if args.tpm != "none" else None
    cmd = qemu_command(vars_path, ssh_port=args.ssh_port, credentials=args.credential,
                       tpm=tpm, tpm_interface=args.tpm)
    print(f"HTTP boot from {url}", file=sys.stderr)
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
