# Cherry

Cherry is a [Buildroot](https://buildroot.org)-based Linux distribution with
just enough OS to run [systemd-nspawn](https://www.freedesktop.org/software/systemd/man/latest/systemd-nspawn.html)
containers.

The whole OS is a single EFI binary, meant to be network-booted with UEFI HTTP
boot. That binary is a [unified kernel image](https://uapi-group.org/specifications/specs/unified_kernel_image/)
(UKI) holding:
- the kernel
- its command line
- the initramfs, holding the root filesystem as a zstd-compressed
  [EROFS](https://erofs.docs.kernel.org) image

Once booted:
- **Identity:** a TPM 2.0 is mandatory. The machine ID and the IPFS node's key
  come from the TPM, so they stay the same across reboots (see
  [Machine identity](#machine-identity)).
- **Root:** the EROFS image, read-only, mounted straight from the initramfs.
  It stays compressed in RAM (see [Memory](#memory)).
- **`/var`:** a tmpfs of up to half the RAM, so nothing persists yet. Disks
  for `/var` and containers come later.
- **Swap:** compressed, in RAM (zram; see [Memory](#memory)).
- **Services:** systemd 258 with networkd (DHCP), resolved, timesyncd,
  journald, machined/machinectl and systemd-nspawn, plus OpenSSH, an IPFS
  node (see [IPFS](#ipfs)), an OpenCode server and OpenChamber, its web
  workspace (see [OpenCode](#opencode) and [OpenChamber](#openchamber)).

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

`scripts/update_packages.py` bumps Cherry's packages to their latest upstream
releases, hashes included: it looks up GitHub and GitLab tags and releases,
the npm registry and Debian's snapshot archive, and has Buildroot download the
new release to hash it. `--check` only reports what is newer; a
`package=version` argument picks a version. It needs git, and npm for
OpenChamber. Then build, run `make test`, and update the sizes below.

## Booting

Serve `cherry-x86_64.efi` from any HTTP server and point the machine's UEFI
HTTP boot at it. There are two common ways:

- **DHCP:** answer requests whose vendor class is `HTTPClient` with the boot
  file URL (option 67) and the vendor class `HTTPClient` (option 60).
- **Firmware setup:** configure the URL in the firmware's setup menu.

The firmware downloads the binary into memory and starts it. A machine needs
RAM for the binary while it boots; then for the root filesystem image, the
files in use, and whatever the containers use (see [Memory](#memory)).

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

`make qemu` and `make test` need:
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
2. **Kernel:** unpacks the initramfs into a ramfs and runs its `/init`,
   [stage 1](package/cherry-stage1/src/stage1.c). The initramfs holds only
   stage 1 and the root filesystem image. (`rootfstype=ramfs`: EROFS can
   mount a file from a ramfs, but not from the default tmpfs.)
3. **Stage 1:** mounts the image read-only, straight from the file, makes it
   the root, and runs its [`/init`](board/x86_64/rootfs-overlay/init), a shell
   script.
4. **`/init`:** does what an initrd would before systemd starts:
   - mounts `/dev`, `/proc`, `/sys` and `/run`
   - derives the machine's identity from the TPM (see
     [Machine identity](#machine-identity))
   - starts systemd with that machine ID, or, without a usable TPM, with
     `cherry-no-tpm.target` instead of the default target
5. **systemd:**
   - mounts a tmpfs on `/var` and fills it from the image's factory defaults
     (Buildroot's `BR2_INIT_SYSTEMD_VAR_FACTORY`)
   - sets up compressed swap in RAM (see [Memory](#memory))

Because `/etc` is read-only:
- SSH host keys are generated under `/var/lib/sshd`, so they change on every
  boot for now
- `/root` is a symlink into `/var`

## Memory

Everything lives in RAM: the root filesystem, `/var` with its containers, and
the memory processes allocate.

The root filesystem stays compressed. Its only copy is the EROFS image in the
initramfs: 79 MB for 215 MiB of files before OpenCode, OpenChamber and Bun,
which add about 175 MB for 450 MiB of files (see [OpenCode](#opencode) and
[OpenChamber](#openchamber)), compressed with zstd in clusters of up to 64 KiB.
The kernel keeps the files in use in its
page cache, decompressed, and drops them under memory pressure; it reads them
from the image again when they are needed.

Under memory pressure, the kernel also compresses the coldest pages into zram,
a swap device in RAM, instead of running out:

- **What it compresses:** process memory, and tmpfs pages too, i.e. cold files
  of `/var`, where containers live.
- **When:** only under memory pressure. Until then, nothing goes to zram.
- **Setup:** `cherry-zram-swap.service` sets up `/dev/zram0` at every boot:
  - compressed with zstd
  - as large as the RAM, uncompressed. zram's page table takes 0.4% of that
    up front: 12 MB on a 3 GB machine.
- **Swap-ins:** swap is in RAM, so `vm.page-cluster` is 0: the kernel reads
  single pages from it rather than clusters of 8.

`/proc/swaps` and `/sys/block/zram0/mm_stat` show how much it holds, and in
how much space.

The image also leaves out what a container host doesn't need:

- **systemd's hardware database (hwdb):** 13.5 MB, which udevd kept mapped.
  - udev adds no vendor, model or keymap properties from it.
  - Two kinds of network device get different names: Dell iDRAC's USB NIC
    isn't called `idrac`, and the names of Microsoft MANA NICs include their
    PCI domain.
  - Cherry's network configuration matches any Ethernet link, whatever its
    name.
- **rpm's build tools,** `rpmbuild` and `rpmspec`, and with them `file` and
  libmagic's 10 MB database. rpm can't be built without them, but Cherry only
  installs packages with it; containers bring their own `rpm-build`.

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

**The IPFS node's key** comes from the TPM too, so its peer ID is the same at
every boot (see [IPFS](#ipfs)). `ipfs-identity.service` has the TPM derive an
HMAC key from its endorsement seed, as it derives the EK, and HMAC a fixed
label with it. The result is the seed of the node's Ed25519 key. Unlike the
EK, that key is secret, but nothing protects it from root yet: anything with
access to the TPM can derive it again.

**Without a usable TPM 2.0,** boot stops: no TPM, a TPM 1.2, or a TPM that
cannot create its EK. `/init` writes the reason to
`/run/cherry/identity/error` and starts systemd with `cherry-no-tpm.target`
instead of the default target. That target brings up only the basic system and
a login prompt on the console, and shows the error on every console.
Networking, sshd, IPFS and containers never start.

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
  path instead of the name. The path needs a `/` (e.g. `./fedora`): a bare
  name is looked up in `/usr/share/rpmstrap`.
- EL8 isn't supported: its repositories need modularity, which Cherry's dnf5
  is built without.
- The host's rpm only installs packages: `rpmbuild` and `rpmspec` aren't on the
  image (see [Memory](#memory)).
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
- **Identity:** the node's key comes from the TPM (see
  [Machine identity](#machine-identity)), so its peer ID is the same at every
  boot. `ipfs-identity.service` creates the repository with that key before
  the daemon starts. If that fails, the daemon doesn't start rather than make
  up a key.
- **Nothing else persists yet:** `/var` is a tmpfs, so the repository is new
  at every boot. Whatever the node added, pinned or cached is gone.
- **Ports:**
  - The swarm listens on port 4001 (TCP, and UDP for QUIC, WebTransport and
    WebRTC) on every address.
  - The RPC API (`127.0.0.1:5001`) and the HTTP gateway (`127.0.0.1:8080`)
    listen on loopback only.
- **Configuration:** Kubo's defaults, as `ipfs init` creates them.
  - `ipfs config` changes them in the repository, so only until the next
    boot. Most changes take effect after `systemctl restart ipfs`.
  - Telemetry is off: Kubo has no endpoint to send it to.
- **QUIC:** `/usr/lib/sysctl.d/50-ipfs.conf` raises the socket buffer limits
  to 7.5 MB, above the 7 MiB that QUIC asks for.

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
  repository directly and leave files owned by root, so the daemon's later
  writes and garbage collection fail with "permission denied".
  `chown -R ipfs:ipfs /var/lib/ipfs` repairs that.
- The repository is in RAM, and Kubo doesn't collect garbage by default.
  `ipfs repo gc` frees blocks that aren't pinned.

## OpenCode

Every Cherry machine runs an [OpenCode](https://opencode.ai) server, as
`opencode.service`: `opencode serve`, the HTTP API and web UI of the AI coding
agent. Like sshd, it starts only on a machine with a TPM.

- **User and state:** the server runs as the `opencode` user, with its state
  under `/var/lib/opencode`, laid out as under a home directory: sessions,
  provider credentials and logs in `.local/share/opencode`, configuration in
  `.config/opencode`. `/var` is a tmpfs, so all of it is new at every boot.
- **Work directory:** `/var/lib/opencode/work`, empty at every boot. Clients
  can pick another directory, but `/var/lib/opencode` is the only place the
  server can write: it sees the rest of the system read-only, with a private
  `/tmp`, and runs without capabilities. Its tools run as the `opencode` user
  under the same restrictions.
- **Port and password:** 4096, on loopback only. OpenCode 2 serves its API
  under `/api` and its web UI on that port, both behind HTTP basic auth as
  user `opencode`. The service makes up the password at every boot and keeps
  it in `/var/lib/opencode/server-password`, readable by root and the
  `opencode` user. `/api/info` answers `{"version":"..."}`, and
  `/openapi.json` describes the [API](https://opencode.ai/v2/docs/api/).
- **Providers:** none are configured; nothing on the machine holds a key.
  Connect one at runtime, in OpenCode's web UI or in OpenChamber (see
  [OpenChamber](#openchamber)). The credential lands in `/var/lib/opencode`,
  so it is gone at the next boot.
- **Tools:** the bash tool runs `bash`, the grep tool the image's ripgrep, and
  git is on the image for its snapshots and worktrees.
- **Binary:** upstream's prebuilt x86_64 build, from the npm platform package
  `@opencode/cli-linux-x64-baseline`: the "baseline" variant that doesn't need
  AVX2, so it runs on VM CPU models without it. It is a Bun single-file
  executable, which stripping breaks: Buildroot's strip skips it
  (`BR2_STRIP_EXCLUDE_FILES`) and `post-build.sh` checks that. It is 200 MiB,
  about 98 MB in the image, and the idle server uses about 170 MB of RAM.
  Automatic updates are off: a new version comes with a new image.

To use it, forward the port over SSH, then open `http://127.0.0.1:4096/` in a
browser, or point OpenCode's own clients at it with `--server` and the
password in `OPENCODE_PASSWORD`:

```sh
ssh -L 4096:127.0.0.1:4096 -L 3000:127.0.0.1:3000 -p 2222 root@localhost  # the VM of make qemu
export OPENCODE_PASSWORD=$(ssh -p 2222 root@localhost cat /var/lib/opencode/server-password)
opencode session list --server http://127.0.0.1:4096
```

## OpenChamber

Every Cherry machine also runs
[OpenChamber](https://github.com/openchamber/openchamber), a web workspace for
running and reviewing the agent's work, as `openchamber.service`: its server,
attached to `opencode.service` with the password above. Like OpenCode, it
starts only on a machine with a TPM.

- **Runtime:** upstream's npm package `@openchamber/web` with its
  dependencies, under `/usr/lib/openchamber`, run on [Bun](https://bun.sh)
  (`/usr/bin/bun`, upstream's prebuilt x86_64 "baseline" build), as upstream's
  own container does. `openchamber` on the command line is its CLI.
- **User and data:** the `opencode` user, with the same home, so its data is in
  `/var/lib/opencode/.config/openchamber`, new at every boot, and it sees the
  system under the same restrictions as OpenCode.
- **Port:** 3000, on loopback only, and no password on the browser UI. Given
  the systemd credential `openchamber.ui_password`, it listens on every
  address instead, with that password: for `make qemu`,
  `python3 scripts/run_qemu.py --credential openchamber.ui_password=...`.
  `/health` reports its version, `/api/opencode/compatibility` the OpenCode it
  is attached to.
- **Attached, not managing:** OpenChamber takes OpenCode as an external
  server. It starts none of its own, and can't restart or upgrade this one.
- **Left out:** local speech recognition and synthesis (sherpa-onnx, 32 MiB,
  plus models downloaded at run time), and tunnels (no cloudflared or ngrok on
  the image). Its update checks and device-pairing relay reach the network.
- **Size:** Bun is 76 MiB, about 37 MB in the image; the npm tree 175 MiB,
  about 41 MB. The idle server uses about 115 MB of RAM.

With the port forwarded as above, open `http://127.0.0.1:3000/`.

## Security notes

This is a development image:
- `root` has no password on the console (Buildroot's default).
- SSH accepts keys only.
- Any local user can control the IPFS node through its RPC API, and its swarm
  port is open to the network: there's no host firewall yet.
- The OpenCode server's password is in `/var/lib/opencode/server-password`,
  readable by root and the `opencode` user. With it, any local user can drive
  the server through its HTTP API, and so run commands as the `opencode` user.
- OpenChamber's browser UI has no password unless the machine was given one
  as a credential: on loopback, any local user can use it, and so run commands
  as the `opencode` user. With the credential, it is open to the network over
  plain HTTP.
- Root can derive the IPFS node's private key from the TPM at any time: it
  isn't bound to the boot state (PCRs) yet.
- The image is not signed.

Secure Boot would mean signing the UKI (`ukify --secureboot-*`). With Secure
Boot enabled, systemd-stub ignores command-line overrides passed by the
firmware.
