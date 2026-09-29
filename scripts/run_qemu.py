#!/usr/bin/env python3
"""Boot a Cherry disk image in QEMU (q35 + OVMF).

The VM state lives in --state: a qcow2 overlay on top of images/disk.img (so
the build output stays pristine and the disk can be larger than the image, to
exercise systemd-repart) and a private copy of the OVMF variable store (the
UEFI boot entries RAUC switches between persist there across runs).

Never add bootindex=/-boot: OVMF would then rewrite BootOrder and prune the
cherry-a/cherry-b entries. Never add pvpanic: QEMU would exit on a kernel panic
instead of resetting, which breaks the panic=10 rollback path.

Also used as a module by tests/smoke.py.
"""

import argparse
import os
import shutil
import subprocess
import sys

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


class State:
    """Paths of one VM's persistent state."""

    def __init__(self, images, state_dir):
        self.images = os.path.abspath(images)
        self.dir = os.path.abspath(state_dir)
        self.disk = os.path.join(self.dir, "disk.qcow2")
        self.vars = os.path.join(self.dir, "OVMF_VARS.fd")

    def prepare(self, disk_size="4G", fresh_vars=False):
        os.makedirs(self.dir, exist_ok=True)
        image = os.path.join(self.images, "disk.img")
        if not os.path.exists(image):
            sys.exit(f"{image} not found; build first")
        if not os.path.exists(self.disk):
            subprocess.run(
                ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw", "-b", image, self.disk, disk_size],
                check=True,
            )
        if fresh_vars or not os.path.exists(self.vars):
            self.reset_vars()

    def reset_vars(self):
        """Start over with empty NVRAM, as on a new machine."""
        shutil.copyfile(find_ovmf()[1], self.vars)


def qemu_command(state, serial="mon:stdio", ssh_port=None, credentials=(), smbios_strings=(), memory="2048", cpus="2"):
    code, _ = find_ovmf()
    cmd = ["qemu-system-x86_64", "-machine", "q35", "-m", str(memory), "-smp", str(cpus)]
    if kvm_usable():
        cmd += ["-accel", "kvm", "-cpu", "host"]
    else:
        cmd += ["-accel", "tcg,thread=multi", "-cpu", "max"]
    cmd += [
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,unit=1,file={state.vars}",
        "-drive", f"if=none,id=disk0,format=qcow2,file={state.disk}",
        "-device", "virtio-blk-pci,drive=disk0",
        "-device", "virtio-rng-pci",
        "-device", "i6300esb",
        "-action", "watchdog=reset",
        "-display", "none",
        "-serial", serial,
    ]
    netdev = "user,model=virtio-net-pci"
    if ssh_port:
        netdev += f",hostfwd=tcp:127.0.0.1:{ssh_port}-:22"
    cmd += ["-nic", netdev]
    # SMBIOS type 11 strings: systemd credentials (io.systemd.credential:k=v)
    # and systemd-stub options (io.systemd.stub.*).
    for key_value in credentials:
        cmd += ["-smbios", f"type=11,value=io.systemd.credential:{key_value}"]
    for value in smbios_strings:
        cmd += ["-smbios", f"type=11,value={value}"]
    if not serial.startswith("mon:"):
        cmd += ["-monitor", "none"]
    return cmd


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images", default="output/cherry_x86_64/images")
    parser.add_argument("--state", default="output/cherry_x86_64/qemu")
    parser.add_argument("--disk-size", default="4G", help="size of the overlay disk (default: %(default)s)")
    parser.add_argument("--fresh-vars", action="store_true", help="start with empty UEFI NVRAM")
    parser.add_argument("--ssh-port", type=int, default=2222, help="host port forwarded to the guest's SSH")
    parser.add_argument("--credential", action="append", default=[], metavar="KEY=VALUE",
                        help="pass a systemd credential via SMBIOS (repeatable)")
    args = parser.parse_args()

    state = State(args.images, args.state)
    state.prepare(args.disk_size, args.fresh_vars)
    cmd = qemu_command(state, ssh_port=args.ssh_port, credentials=args.credential)
    print("Serial console on this terminal; Ctrl-A x quits, Ctrl-A c toggles the QEMU monitor.", file=sys.stderr)
    print(" ".join(cmd), file=sys.stderr)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
