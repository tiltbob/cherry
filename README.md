# Cherry

Cherry is a [Buildroot](https://buildroot.org)-based Linux distribution with
just enough OS to run [systemd-nspawn](https://www.freedesktop.org/software/systemd/man/latest/systemd-nspawn.html)
containers.

The whole OS is a single EFI binary, meant to be network-booted with UEFI HTTP
boot. That binary is a [unified kernel image](https://uapi-group.org/specifications/specs/unified_kernel_image/)
(UKI) holding:
- the kernel
- its command line
- the root filesystem, as Buildroot's zstd-compressed cpio used as the
  initramfs

Once booted:
- **Identity:** a TPM 2.0 is mandatory. The machine ID comes from the TPM's
  endorsement key, so it stays the same across reboots (see
  [Machine identity](#machine-identity)).
- **Root:** systemd runs directly from the initramfs as PID 1 and remounts it
  read-only.
- **`/var`:** a tmpfs, so nothing persists yet. Disks for `/var` and containers
  come later.
- **Services:** systemd 258 with networkd (DHCP), resolved, timesyncd,
  journald, machined/machinectl and systemd-nspawn, plus OpenSSH and an IPFS
  node (see [IPFS](#ipfs)).

Cherry targets x86_64 UEFI machines and VMs with a TPM 2.0.

## Building

A Buildroot host with the [usual prerequisites](https://buildroot.org/downloads/manual/manual.html#requirement)
(on Debian/Ubuntu: `build-essential file cpio unzip rsync bc wget git python3 libncurses-dev`):

```sh
git clone --recurse-submodules https://github.com/tiltbob/cherry
cd cherry
make        # output/cherry_x86_64/images/cherry-x86_64.efi
```

Buildroot 2026.08 is a git submodule. This repository is a
`BR2_EXTERNAL` tree, and the top-level `Makefile` wraps Buildroot:

| Command | Purpose |
|---|---|
| `make` | configure (first time) and build |
| `make menuconfig`, `make savedefconfig`, `make linux-menuconfig` | Buildroot configuration |
| `make br-<target>` | any Buildroot target, e.g. `make br-systemd-rebuild` |
| `make qemu` | UEFI HTTP boot in QEMU + OVMF, with a software TPM |
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
RAM for the binary plus the unpacked root filesystem, plus whatever the containers
use.

The machine needs a TPM 2.0, turned on in the firmware setup: a discrete TPM,
or the firmware TPM (Intel PTT, AMD fTPM). A VM needs a virtual TPM.

`make qemu` does all of this locally:
- serves `images/` on a free port
- writes an OVMF variable store with an HTTP boot entry for
  `http://10.0.2.2:<port>/cherry-x86_64.efi`
- starts a software TPM, swtpm, whose state in
  `output/cherry_x86_64/qemu/tpm` makes the VM the same machine on every run.
  Delete it for a new machine.
- boots QEMU with the serial console on your terminal

It needs:
- `qemu-system-x86_64`
- OVMF and swtpm (`apt install qemu-system-x86 ovmf swtpm`)
- `virt-fw-vars`: `apt install python3-virt-firmware`, or
  `pip install virt-firmware` (set `VIRT_FW_VARS` to use a specific copy)

The TPM is attached through CRB, as firmware TPMs are. Pass `--tpm tis` to
`scripts/run_qemu.py` for the TIS interface of discrete TPMs, or `--tpm none`
to see Cherry stop without a TPM.

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
2. **Kernel:** unpacks the initramfs (the whole root filesystem) into a tmpfs
   and runs [`/init`](board/x86_64/rootfs-overlay/init), a shell script.
3. **`/init`:** does what an initrd would before systemd starts:
   - mounts `/dev`, `/proc`, `/sys` and `/run`
   - derives the machine's identity from the TPM (see
     [Machine identity](#machine-identity))
   - starts systemd with that machine ID, or, without a usable TPM, with
     `cherry-no-tpm.target` instead of the default target
4. **systemd:**
   - remounts `/` read-only (Buildroot's `/etc/fstab`)
   - mounts a tmpfs on `/var` and fills it from the image's factory defaults
     (Buildroot's `BR2_INIT_SYSTEMD_VAR_FACTORY`)

Because `/etc` is read-only:
- SSH host keys are generated under `/var/lib/sshd`, so they change on every
  boot for now
- `/root` is a symlink into `/var`

The root filesystem takes its uncompressed size in RAM.

## Machine identity

Every Cherry machine boots the same image. What tells one machine from another
is its TPM's endorsement key (EK), which the TPM derives from a seed that
survives TPM clears. `/init` creates the TCG default EK, an ECC NIST P-256 key,
with `tpm2_createek`. If the TPM holds an EK template in the TCG template NV
index, `/init` uses that template instead, as the TCG EK profile requires. Then:

- **EK hash:** the SHA-256 of the EK's public key (its DER
  SubjectPublicKeyInfo), in lowercase hex. It names the machine.
- **Machine ID:** the first 32 hex digits of the EK hash. `/init` passes it to
  systemd as `--machine-id=`, so it is in place before systemd starts anything.
  It is stable across reboots, so DHCP leases and journal IDs are too.

`/init` leaves the EK and its hash in `/run/cherry/identity/` (`ek.der`,
`ek-hash`) for later boot steps. Both identify the machine but are not secret:
anyone with access to the TPM can read the EK.

**Without a usable TPM 2.0,** boot stops: no TPM, a TPM 1.2, or a TPM that
cannot create its EK. `/init` writes the reason to
`/run/cherry/identity/error` and starts systemd with `cherry-no-tpm.target`
instead of the default target. That target brings up only the basic system and
a login prompt on the console, and shows the error on every console.
Networking, sshd, config, secrets and containers never start.

## Running containers

Put container trees in `/var/lib/machines`, then start them with
`machinectl start <name>` or run `systemd-nspawn` directly.

With `--network-veth` (or `VirtualEthernet=yes`), systemd-networkd serves DHCP
to the container and masquerades its traffic.

Everything lives in memory for now. Containers and their configuration are
gone after a reboot.

### Debian containers with debootstrap

`debootstrap` installs Debian into a directory and verifies the archive's
signatures against the Debian keyring in `/usr/share/keyrings`:

```sh
debootstrap --variant=minbase --include=systemd,systemd-sysv,dbus trixie /var/lib/machines/trixie
machinectl start trixie
machinectl shell trixie
```

- The `--include` packages let `machinectl start` boot the container and
  `machinectl shell` enter it. Without them, `systemd-nspawn -D <dir>` still
  gives you a shell.
- To bring up the container's veth link, run
  `systemctl enable --now systemd-networkd` inside it.
- The default mirror is `http://deb.debian.org/debian`. To use another one,
  HTTP or HTTPS, pass it as the third argument.
- The tree above uses about 240 MB of RAM.
- Ubuntu and other derivatives need their own keyring: pass
  `--keyring=<file>`.

### RPM-based containers with rpmstrap

`rpmstrap` installs Fedora, CentOS Stream, AlmaLinux or Rocky Linux into a
directory with dnf5. Every package is checked against the distribution's
signing key from `/usr/share/distribution-gpg-keys`:

```sh
rpmstrap fedora 44 /var/lib/machines/fedora
machinectl start fedora
machinectl shell fedora
```

- `rpmstrap -l` lists the distributions and the packages each one installs
  by default: enough to boot the container, enter it with `machinectl shell`
  and run dnf.
  - Package names after the directory replace that default set.
  - Arguments starting with `-` are passed to dnf5, e.g.
    `--setopt=install_weak_deps=True` (weak dependencies are off by default).
- The Fedora set includes systemd-networkd. To bring up the container's veth
  link, run `systemctl enable --now systemd-networkd` inside it.
- The repositories are in `/usr/share/rpmstrap/<distro>/*.repo`. To use
  another mirror or distribution, copy that directory, edit it, and pass its
  path instead of the name.
- EL8 isn't supported: its repositories need modularity, which Cherry's dnf5
  is built without.
- The Fedora tree above uses about 195 MB of RAM, and EL 9 or 10 trees about
  270 MB. While installing, rpmstrap needs about 180 MB more for repository
  metadata and packages, and deletes them afterwards.

### Arch Linux containers with pacstrap

`pacstrap` installs Arch Linux into a directory with pacman, which verifies
every package against the Arch Linux keyring:

```sh
systemctl start pacman-init    # waits until the keyring is ready
pacstrap -K -c /var/lib/machines/arch base
rm -rf /var/cache/pacman/pkg/*
machinectl start arch
machinectl shell arch
```

- `pacman-init.service` builds the host's keyring in `/var/lib/pacman/gnupg`
  at every boot, from the Arch Linux keyring in the image. Boot doesn't wait
  for it, so start it before the first `pacstrap`.
- `-K` gives the container a keyring of its own. Without it, pacstrap copies
  the host's, including the host's local signing key.
- `-c` downloads the packages to the host's `/var/cache/pacman/pkg` instead of
  the container's. They are only needed during the install, so clear them.
- Repositories and mirrors come from `/etc/pacman.conf` and
  `/etc/pacman.d/mirrorlist`, which are read-only. To use others, pass
  `-C <pacman.conf>`, plus `-M` to keep the container's own mirrorlist rather
  than the host's.
- To bring up the container's veth link, run
  `systemctl enable --now systemd-networkd systemd-resolved` inside it.
- `base` uses about 600 MB of RAM, plus about 125 MB for the package cache
  until you clear it.
- Packages signed by a key newer than the image's keyring still install: pacman
  fetches the key over WKD and checks it against the Arch Linux master keys.
- Unlike upstream, Cherry's `pacstrap` and `arch-chroot` give the new root an
  empty `/run` rather than the host's. pacman's hooks run `systemd-tmpfiles`
  in the new root, which would otherwise give the host's `/run/systemd/netif`
  to Arch's `systemd-network` user and stop the host's systemd-networkd from
  writing its state.

## IPFS

Every Cherry machine runs an [IPFS](https://ipfs.tech) node:
[Kubo](https://github.com/ipfs/kubo), as `ipfs.service`. Like sshd, it starts
only on a machine with a TPM.

- **User and repository:** the daemon runs as the `ipfs` user, with its
  repository in `/var/lib/ipfs`.
- **Nothing persists yet:** `/var` is a tmpfs, so the daemon creates a new
  repository at every boot. The node gets a new peer ID, and whatever it
  added, pinned or cached is gone.
- **Ports:**
  - The swarm listens on port 4001 (TCP, and UDP for QUIC, WebTransport and
    WebRTC) on every address.
  - The RPC API (`127.0.0.1:5001`) and the HTTP gateway (`127.0.0.1:8080`)
    listen on loopback only.
- **Configuration:** Kubo's defaults, created by `ipfs daemon --init`.
  - `ipfs config` changes them in the repository, so only until the next
    boot. Most changes take effect after `systemctl restart ipfs`.
  - Telemetry is off: Kubo has no endpoint to send it to.
- **QUIC:** `/usr/lib/sysctl.d/50-ipfs.conf` raises the socket buffer limits
  to the 7.5 MB that QUIC asks for.

Login shells set `IPFS_PATH=/var/lib/ipfs`, so `ipfs` commands reach the
daemon through its RPC API:

```sh
ipfs id
echo hello | ipfs add -Q                        # prints the CID
ipfs cat <cid>
wget -q -O - http://127.0.0.1:8080/ipfs/<cid>   # the same, through the gateway
```

- Elsewhere, for example in `ssh <host> ipfs ...`, set `IPFS_PATH` yourself
  or pass `--api /ip4/127.0.0.1/tcp/5001`.
- Run `ipfs` commands only while the daemon runs. Without it, they open the
  repository directly, and files they create as root lock the daemon out.
- The repository is in RAM, and Kubo doesn't collect garbage by default.
  `ipfs repo gc` frees blocks that aren't pinned.

## Security notes

This is a development image:
- `root` has no password on the console (Buildroot's default).
- SSH accepts keys only.
- Any local user can control the IPFS node through its RPC API, and its swarm
  port is open to the network: there's no host firewall yet.
- The image is not signed.

Secure Boot would mean signing the UKI (`ukify --secureboot-*`). With Secure
Boot enabled, systemd-stub ignores command-line overrides passed by the
firmware.
