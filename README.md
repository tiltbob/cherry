# Cherry

Cherry is a [Buildroot](https://buildroot.org)-based Linux distribution with
just enough OS to run [systemd-nspawn](https://www.freedesktop.org/software/systemd/man/latest/systemd-nspawn.html)
containers. The host is an immutable, A/B-updated appliance. Everything that
changes lives on a persistent data partition: containers, their configuration,
and host settings.

- **Host:** systemd 258 with networkd, resolved, timesyncd and journald,
  systemd-nspawn with machined/machinectl, and OpenSSH.
- **Updates:** [RAUC](https://rauc.io) signed verity bundles, installable over HTTP (streaming).
- **Boot:** direct UEFI boot, with no bootloader. The firmware boots one
  [UKI](https://uapi-group.org/specifications/specs/unified_kernel_image/) per
  slot, and RAUC's `efi` backend switches slots through the firmware's
  `BootNext`/`BootOrder` variables.
- **Target:** x86_64 UEFI machines and VMs.

## Building

A Buildroot host with the [usual prerequisites](https://buildroot.org/downloads/manual/manual.html#requirement)
(on Debian/Ubuntu: `build-essential file cpio unzip rsync bc wget git python3 libncurses-dev`):

```sh
git clone --recurse-submodules https://github.com/tiltbob/cherry
cd cherry
make keys   # once: development RAUC signing key in keys/ (gitignored)
make        # output/cherry_x86_64/images/
```

Buildroot 2026.02.3 LTS is a git submodule. This repository is a
`BR2_EXTERNAL` tree, and the top-level `Makefile` wraps Buildroot:

| Command | Purpose |
|---|---|
| `make` | configure (first time) and build |
| `make menuconfig`, `make savedefconfig`, `make linux-menuconfig` | Buildroot configuration |
| `make br-<target>` | any Buildroot target, e.g. `make br-rauc-rebuild` |
| `make qemu` | boot the image in QEMU + OVMF (`apt install qemu-system-x86 qemu-utils ovmf`) |
| `make test` | end-to-end smoke test in QEMU ([tests/smoke.py](tests/smoke.py)) |
| `make clean` | remove `output/cherry_x86_64` |

Downloads go to `dl/` and ccache to `.ccache/`. Override the locations with
`BR2_DL_DIR` and `BR2_CCACHE_DIR`.

The build produces the following in `output/cherry_x86_64/images/`:

| File | Contents |
|---|---|
| `disk.img` | the complete disk image, to write to a disk or boot in a VM |
| `cherry-x86_64.raucb` | the signed update bundle |
| `cherry-a.efi`, `cherry-b.efi` | the per-slot UKIs |
| `rootfs.squashfs`, `efi.vfat` | the slot images |

### Signing keys

The image trusts exactly one keyring. RAUC only installs bundles signed by a
certificate that chains to it. `make keys` creates a self-signed development
key and certificate under `keys/`. For anything else, point the build at your
own PKI (paths or PKCS#11 URIs):

```sh
make CHERRY_RAUC_KEY=... CHERRY_RAUC_CERT=... CHERRY_RAUC_KEYRING=...
```

## Disk layout

| # | Partition (GPT name) | Type | Size | Contents |
|---|---|---|---|---|
| 1 | `cherry-efi-a` | ESP | 128 MiB | UKIs for slot A |
| 2 | `cherry-efi-b` | ESP | 128 MiB | UKIs for slot B (empty until the first update) |
| 3 | `cherry-root-a` | root-x86-64 | 512 MiB | read-only squashfs root, slot A |
| 4 | `cherry-root-b` | root-x86-64 | 512 MiB | slot B (empty until the first update) |
| 5 | `cherry-data` | Linux data | rest of the disk | ext4 `/var` |

A slot is an ESP plus a root filesystem, and RAUC always updates the pair
together. On first boot, `systemd-repart` grows `cherry-data` to fill the disk,
and systemd grows the filesystem online.

## How it boots

Each slot's ESP holds `\EFI\cherry\cherry-a.efi` and `\EFI\cherry\cherry-b.efi`.
These are identical kernels whose embedded command lines differ only in
`root=PARTLABEL=cherry-root-X rauc.slot=cherry-X`.

The UEFI boot entries `cherry-a` and `cherry-b` each point at their own slot's
UKI and carry no load options. `cherry-efi-entries.service` creates or repairs
them on every boot, because RAUC never creates entries.

1. **First boot** (a freshly written disk, so NVRAM is empty): the firmware
   falls back to `\EFI\BOOT\BOOTX64.EFI` on the first ESP. That is slot A's UKI.
   The boot entries are created, and once the boot is healthy, slot A is
   promoted to the front of `BootOrder`.
2. **Update:** `rauc install <bundle or URL>` does three things in order:
   - marks the other slot bad (removes it from `BootOrder`)
   - writes its root filesystem and ESP
   - sets `BootNext`

   The next boot tries the new slot once.
3. **Promotion:** when the new slot reaches `boot-complete.target`,
   `cherry-mark-good.service` runs `rauc status mark-good`, which moves the slot
   to the front of `BootOrder`. `boot-complete.target` requires
   `cherry-health.service`, which checks that:
   - the command line names one slot, and the kernel, root filesystem and
     ESP all belong to it
   - `/var` and `/etc` are mounted as expected

   It also requires `systemd-boot-check-no-failures.service`, which checks
   that no unit failed.
4. **Rollback:** the firmware consumes `BootNext`, so any reset goes back to the
   previous slot. A reset happens in any of these cases:
   - a kernel panic (`panic=10`)
   - a hang, caught by the 30 s hardware watchdog
   - a trial boot that is still unconfirmed after 15 minutes, which
     `cherry-trial-deadline.timer` reboots

`rauc install` refuses to run unless the current boot is healthy and
confirmed.

## Persistent state

The root filesystem is read-only. `/usr/lib/cherry/init` runs before systemd:

1. It checks and mounts `cherry-data` on `/var`.
2. It mounts a writable overlay on `/etc`, whose upper layer is in
   `/var/lib/cherry/etc`.

So host configuration persists across updates, for example `machinectl enable`,
`.nspawn` files, network configuration, SSH host keys and the machine ID. Files
you change in `/etc` shadow the image's version from then on. Cherry's own
configuration therefore lives in `/usr/lib` (for example
`/usr/lib/rauc/system.conf`, or `/usr/lib/systemd/network`). `/root` and `/home`
live on `/var`. To reset all host configuration, run
`touch /var/lib/cherry/factory-reset` and reboot.

## Running containers

Put container trees or images in `/var/lib/machines`. Configure them with
`/etc/systemd/nspawn/<name>.nspawn`, then start them with
`machinectl start <name>` or `machinectl enable <name>`. With `--network-veth`
(or `VirtualEthernet=yes`), systemd-networkd serves DHCP to the container and
masquerades its traffic.

Cherry does not provide container images. Unpack a root filesystem tarball
into `/var/lib/machines/<name>`.

## Security notes

The default image is a development image, and this applies to both the console
and SSH:

- **Console:** `root` logs in on the console without a password (Buildroot's
  default).
- **SSH:** allows key-based root login only. Put keys in
  `/root/.ssh/authorized_keys`, which persists on `/var`.

For real deployments:
- Set `BR2_TARGET_GENERIC_ROOT_PASSWD`.
- Use your own signing PKI.
- Plan for Secure Boot, e.g. by signing the UKIs with `ukify --secureboot-*`.
  The layout does not change, because the command lines are already embedded
  in the UKIs.

## Known limitations

- **Hang before systemd starts:** a hang before systemd arms the watchdog is
  only recovered by a power cycle.
- **NVRAM loss** (CMOS reset, or moving the disk to another machine): the
  machine falls back to slot A, even if slot B was the current one.
- **Two Cherry disks in one machine:** partition names are fixed, so this is
  ambiguous.
