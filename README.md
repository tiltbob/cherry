# Cherry

Cherry is a [Buildroot](https://buildroot.org)-based Linux distribution with
just enough OS to run [systemd-nspawn](https://www.freedesktop.org/software/systemd/man/latest/systemd-nspawn.html)
containers.

The whole OS is a single EFI binary, meant to be network-booted with UEFI HTTP
boot. That binary is a [unified kernel image](https://uapi-group.org/specifications/specs/unified_kernel_image/)
(UKI) holding:
- the kernel
- its command line
- an initramfs that carries the root filesystem as a squashfs image

Once booted:
- **Root:** the read-only squashfs.
- **`/var`:** a tmpfs, so nothing persists yet. Disks for `/var` and containers
  come later.
- **Services:** systemd 258 with networkd (DHCP), resolved, timesyncd,
  journald, machined/machinectl and systemd-nspawn, plus OpenSSH.

Cherry targets x86_64 UEFI machines and VMs.

## Building

A Buildroot host with the [usual prerequisites](https://buildroot.org/downloads/manual/manual.html#requirement)
(on Debian/Ubuntu: `build-essential file cpio unzip rsync bc wget git python3 libncurses-dev`):

```sh
git clone --recurse-submodules https://github.com/tiltbob/cherry
cd cherry
make        # output/cherry_x86_64/images/cherry-x86_64.efi
```

Buildroot 2026.02.3 LTS is a git submodule. This repository is a
`BR2_EXTERNAL` tree, and the top-level `Makefile` wraps Buildroot:

| Command | Purpose |
|---|---|
| `make` | configure (first time) and build |
| `make menuconfig`, `make savedefconfig`, `make linux-menuconfig` | Buildroot configuration |
| `make br-<target>` | any Buildroot target, e.g. `make br-systemd-rebuild` |
| `make qemu` | UEFI HTTP boot in QEMU + OVMF |
| `make test` | end-to-end smoke test in QEMU ([tests/smoke.py](tests/smoke.py)) |
| `make clean` | remove `output/cherry_x86_64` |

Downloads go to `dl/` and ccache to `.ccache/`. Override the locations with
`BR2_DL_DIR` and `BR2_CCACHE_DIR`.

## Booting

Serve `cherry-x86_64.efi` from any HTTP server and point the machine's UEFI
HTTP boot at it. There are two common ways:

- **DHCP:** answer requests whose vendor class is `HTTPClient` with the boot
  file URL (option 67) and the vendor class `HTTPClient` (option 60).
- **Firmware setup:** configure the URL in the firmware's setup menu.

The firmware downloads the binary into memory and starts it. A machine needs
roughly 3× the binary's size in RAM to boot, plus whatever the containers use.

`make qemu` does all of this locally:
- serves `images/` on a free port
- writes an OVMF variable store with an HTTP boot entry for
  `http://10.0.2.2:<port>/cherry-x86_64.efi`
- boots QEMU with the serial console on your terminal

It needs:
- `qemu-system-x86_64`
- OVMF (`apt install qemu-system-x86 ovmf`)
- `virt-fw-vars`: `apt install python3-virt-firmware`, or
  `pip install virt-firmware` (set `VIRT_FW_VARS` to use a specific copy)

SSH is forwarded from host port 2222. `root` logs in without a password on the
console. To log in over SSH, pass a key as a
[systemd credential](https://systemd.io/CREDENTIALS/):

```sh
python3 scripts/run_qemu.py --credential "ssh.authorized_keys.root=$(cat ~/.ssh/id_ed25519.pub)"
ssh -p 2222 root@localhost
```

## How it starts

1. **Firmware:** downloads and starts the UKI. systemd-stub then boots the
   kernel with the embedded command line and initramfs.
2. **initramfs:** holds `rootfs.squashfs` and a small static `/init`
   ([package/cherry-init](package/cherry-init/src/init.c)). `/init` attaches the
   image to a read-only loop device, mounts it, makes it the root filesystem and
   executes systemd.
3. **systemd:** mounts a tmpfs on `/var` and fills it from the image's factory
   defaults (Buildroot's `BR2_INIT_SYSTEMD_VAR_FACTORY`).

Because `/etc` is read-only:
- the machine ID is generated on every boot
- SSH host keys are generated under `/var/lib/sshd`
- `/root` is a symlink into `/var`

## Running containers

Put container trees in `/var/lib/machines`, then start them with
`machinectl start <name>` or run `systemd-nspawn` directly.

With `--network-veth` (or `VirtualEthernet=yes`), systemd-networkd serves DHCP
to the container and masquerades its traffic.

Everything lives in memory for now. Containers and their configuration are
gone after a reboot.

## Security notes

This is a development image:
- `root` has no password on the console (Buildroot's default).
- SSH accepts keys only.
- The image is not signed.

Secure Boot would mean signing the UKI (`ukify --secureboot-*`). With Secure
Boot enabled, systemd-stub ignores command-line overrides passed by the
firmware.
