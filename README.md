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
- **Identity:** a TPM 2.0 is mandatory. The machine ID comes from the TPM, and
  the hostname from the machine ID, so they stay the same across reboots (see
  [Machine identity](#machine-identity)).
- **Secure Boot:** a machine whose firmware is in setup mode enrolls Cherry's
  keys, whose root is a Nitrokey HSM 2, and reboots with Secure Boot on;
  releases are signed for it (see [Secure Boot](#secure-boot)).
- **Root:** the EROFS image, read-only, mounted straight from the initramfs.
  It stays compressed in RAM (see [Memory](#memory)).
- **`/var`:** the volume `var` of the pool `cherry`, the machine's own: on
  the disks the agent gave it, which the TPM unlocks at boot, where
  containers persist. Until the pool has disks, it is in RAM, 128 MiB, so
  the agent runs and nothing persists (see [Disks](#disks)).
- **Storage:** the agent claims the machine's disks, which erases them, and
  makes LVM pools of them, `cherry` first, with SSDs as hard disks' cache,
  and volumes, each disk a LUKS2 volume the TPM unlocks (see
  [Storage](#storage)).
- **Swap:** compressed, in RAM (zram; see [Memory](#memory)).
- **Firewall:** nothing comes in unasked but ping, to the host or to its
  containers (see [Firewall](#firewall)).
- **Services:** systemd 258, built with seccomp so that units' system call
  filters apply, with networkd (DHCP), resolved, timesyncd, journald,
  machined/machinectl and systemd-nspawn, and OpenChamber running the
  OpenCode agent, as the `cherry` user (see [Cherry service](#cherry-service)),
  with tools to build and run container images, which never leave the
  machine (see [Building images](#building-images)). There is no SSH: a
  machine is managed through OpenChamber, paired from its console.
- **Two images:** the dev image, whose `root` logs in on the console, and the
  prod image, which releases are, with root locked out (see
  [Building](#building)).

Cherry targets x86_64 UEFI machines and VMs with a TPM 2.0.

## Building

A Buildroot host with the [usual prerequisites](https://buildroot.org/downloads/manual/manual.html#requirement)
(on Debian/Ubuntu: `build-essential file cpio unzip rsync bc wget git python3 libncurses-dev`):

```sh
git clone --recurse-submodules https://github.com/tiltbob/cherry
cd cherry
make        # output/cherry_x86_64/images/cherry-x86_64.efi, and cherry-x86_64-prod.efi
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
| `make check` | fast checks that need no build (below) |
| `make clean` | remove `output/cherry_x86_64` |

**Two images come out of a build,** from the one root filesystem:
`cherry-x86_64.efi`, the dev image, whose `root` logs in on the console
without a password, for `make qemu` and the smoke test; and
`cherry-x86_64-prod.efi`, the prod image, the same but for `cherry.prod` on
its kernel command line, which has `/init` lock root out: root's password
field becomes `*`, in copies of `/etc/passwd` and `/etc/shadow` bound over
the read-only originals, and the console refuses `root` without asking for a
password, with the `agetty.autologin` credential as without it. The command
line is the UKI's, which the firmware cannot change and PCR 11 measures, so
prod is a property of the signed image. Releases are the prod image: a
release machine has no root, and is managed through OpenChamber alone (see
[Cherry service](#cherry-service)).

The image takes its version (`/usr/lib/os-release`, the boot banner) from
`git describe` and the nearest release tag: `v0.0.1` for a build of the
release's commit, `v0.0.1-3-g<commit>` for one three commits later, with
`-dirty` while tracked files have uncommitted changes, and `g<commit>` in a
clone without the tags. `make BR2_EXTERNAL_CHERRY_VERSION=<name>` names it
otherwise.

The build signs the image's PCR 11 policy, which binds a machine's disks to
the image (see [Disks](#disks)), with a key of the build tree's own,
`output/cherry_x86_64/pcr.key`, made at the first build and kept: a rebuilt
image still unlocks the disks a VM sealed under the last one. `make clean`
takes the key with it, and those disks then unlock under no image, until
`wipefs` makes them blank. Releases are signed with the repository's PCR key
instead (see [Secure Boot](#secure-boot)).

Downloads go to `dl/` and ccache to `.ccache/`. Override the locations with
`BR2_DL_DIR` and `BR2_CCACHE_DIR`. Buildroot never deletes a download:
`python3 scripts/prune_dl.py` lists what in `dl/` no configuration needs any
more, old versions and vendored tarballs, and `--delete` removes it. CI does
that before saving its download cache, which it saves, like its ccache, only
from `main`: GitHub shows a cache to the branch that saved it and, from
`main`, to every branch, within 10 GB per repository. The `sdk` workflow keeps
a download cache of its own, pruned to the SDK configuration; neither workflow
restores the other's downloads.

The main defconfig doesn't build its cross toolchain: it downloads it, as an
external toolchain, from a [GitHub release](https://github.com/tiltbob/cherry/releases)
named `sdk-<version>` (`BR2_TOOLCHAIN_EXTERNAL_URL`; its SHA-256 is in
`patches/toolchain-external-custom/toolchain-external-custom.hash`). That
saves the toolchain's build, about ten minutes with a warm ccache and an hour
without, on every build; Buildroot still checks that the toolchain's gcc,
kernel headers and C library are the ones the defconfig says.

The toolchain itself comes from `configs/cherry_sdk_x86_64_defconfig`, which
has the toolchain options and nothing else: `make sdk` builds it, as
Buildroot's relocatable SDK, into
`output/cherry_sdk_x86_64/images/cherry-sdk-x86_64.tar.gz`, to unpack anywhere
and fix up with its `relocate-sdk.sh`. The SDK defconfig picks no `/bin/sh`
either: without busybox, Buildroot's default is bash, which would put readline
and ncurses in the SDK's sysroot. Pushing a tag `sdk-<version>`, or running
the `sdk` workflow by hand with that name as its input, has CI build it and
publish it as the GitHub release of that name, with its SHA-256
(`.github/workflows/sdk.yml`). To change the toolchain: edit the SDK
defconfig, publish a release (`sdk-YYYY.MM.DD.NNN`, with NNN counting the
day's releases), then point the main defconfig's URL and the hash file at it.

`make check` runs the checks that need no build, in about a minute, and CI
runs them before every build (`.github/workflows/build.yml`): Buildroot's
`check-package` over `Config.in`, `package/` and `configs/` (with shellcheck
on the scripts), flake8 over `tests/` and `scripts/`, the workflows as YAML,
`sh -n` over every shell script, `systemd-analyze verify` over every unit
against a root laid out as the image (so a unit's `ExecStart=` must name a
script where its package installs it), and tests of their own under `tests/`:
the smoke test's scenario list, the polkit rules, `cherry-bootstrap`'s
argument checks and tool calls, `cherry-image`'s builds from a recipe, in a
busybox root, the OpenCode MCP servers' tools and the Environments
extension's manifest and service against stand-ins for systemctl,
machinectl, busctl and ip, `cherry-storage`'s claims, LVM calls, sealing of
the keys and `/var` from the pool, in RAM and on its disks, against
stand-ins for LVM, the disks' tools and sysfs, `post-build.sh`'s check of
`opencode`, the firewall's rules, in network namespaces standing in for a
host, its LAN and its containers, the Secure Boot keys in
`board/x86_64/secureboot/` and what `cherry-secureboot` enrolls from them, the
key ceremony on a SoftHSM token, the UKI's PCR policy as `scripts/pcrsign.py`
computes, signs and checks it, against systemd's vectors, and the image's
version from the release tags. `make -k check` runs on after a failure; `make
check-<name>` runs one. They need shellcheck, flake8, python3-magic,
python3-yaml, systemd's `systemd-analyze`, openssl and util-linux's flock;
node for the polkit rules, the MCP servers and the extension; busybox and
user namespaces for `cherry-image` and the firewall, and nft and ip for the
firewall; and softhsm2 and opensc for the ceremony (each skipped without
them).

`scripts/update_packages.py` bumps Cherry's packages to their latest upstream
releases, hashes included: it looks up GitHub and GitLab tags and releases,
the npm registry and Debian's snapshot archive, and has Buildroot download the
new release to hash it. `--check` only reports what is newer; a
`package=version` argument picks a version. It needs git, and npm for
OpenChamber. Then build, run `make test`, and update the sizes below.

## Booting

The image is `output/cherry_x86_64/images/cherry-x86_64.efi` or
`cherry-x86_64-prod.efi` from a build (see [Building](#building)), or the
asset of a [GitHub release](https://github.com/tiltbob/cherry/releases), the
prod image: CI builds every published release from its tag and, once the
smoke test passed, signs it for Secure Boot (see [Secure Boot](#secure-boot))
and attaches it, as `cherry-x86_64.efi`, with its SHA-256. To cut a release
without a tag, run the `build` workflow by hand on `main` with the release's
name, `v<version>`, as its input: the run builds the image under that name,
tests it, then creates the release and its tag on the commit it built.

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
  `http://10.0.2.2:<port>/cherry-x86_64.efi` (`--prod`: the prod image)
- starts a software TPM, swtpm, whose state in
  `output/cherry_x86_64/qemu/tpm` makes the VM the same machine on every
  run. Delete it for a new machine.
- boots QEMU with the serial console on your terminal

`make qemu` and `make test` need:
- `qemu-system-x86_64`
- OVMF and swtpm (`apt install qemu-system-x86 ovmf swtpm`)
- `virt-fw-vars`: `apt install python3-virt-firmware`, or
  `pip install virt-firmware` (set `VIRT_FW_VARS` to use a specific copy)
- for the smoke test's Secure Boot scenario, `sbsign` (`apt install
  sbsigntool`) and openssl

The TPM is attached through CRB, as firmware TPMs are. Pass `--tpm tis` to
`scripts/run_qemu.py` for the TIS interface of discrete TPMs, or `--tpm none`
to see Cherry stop without a TPM. `--secure-boot` boots OVMF's Secure Boot
build instead, in setup mode at first (see [Secure Boot](#secure-boot)).

On the dev image, `root` logs in without a password on the console; the prod
image (`python3 scripts/run_qemu.py --prod`) refuses it (see
[Building](#building)). There is no SSH: the firewall lets nothing in (see
[Firewall](#firewall)), and the machine is managed through OpenChamber, paired
from the console (see [Cherry service](#cherry-service)). `--credential
KEY=VALUE` passes a [systemd credential](https://systemd.io/CREDENTIALS/)
through SMBIOS, as a platform would.

## How it starts

1. **Firmware:** downloads and starts the UKI. systemd-stub measures the
   UKI's sections into PCR 11 of the TPM, then boots the kernel with the
   embedded command line and initramfs, adding the UKI's PCR signature and
   public key to the latter.
2. **Kernel:** unpacks the initramfs into a ramfs and runs its `/init`,
   [cherry-pit](package/cherry-pit/src/cherry-pit.c). The initramfs holds
   only cherry-pit and the root filesystem image. (`rootfstype=ramfs`: EROFS
   can mount a file from a ramfs, but not from the default tmpfs.)
3. **cherry-pit:** mounts the image read-only, straight from the file, gives
   it a tmpfs `/run` with the PCR signature and public key in
   `/run/systemd/`, where systemd's tools look for them, makes it the root,
   and runs its [`/init`](board/x86_64/rootfs-overlay/init), a shell script.
4. **`/init`:** does what an initrd would before systemd starts:
   - mounts `/dev`, `/proc` and `/sys`
   - derives the machine's identity from the TPM (see
     [Machine identity](#machine-identity))
   - on the prod image, locks root out: copies of `/etc/passwd` and
     `/etc/shadow` with root's password field `*`, bound over the read-only
     files (see [Building](#building))
   - starts systemd with that machine ID, or, without a usable TPM, with
     `cherry-no-tpm.target` instead of the default target
5. **systemd:**
   - mounts `/var`: the volume `var` of the pool `cherry`, on the disks the
     TPM unlocks under this image, or made in RAM while the pool has none
     (see [Disks](#disks))
   - fills `/var` with what it lacks of the image's factory defaults
     (Buildroot's `BR2_INIT_SYSTEMD_VAR_FACTORY`)
   - sets up compressed swap in RAM (see [Memory](#memory))
   - loads the firewall before networkd configures any link (see
     [Firewall](#firewall))
   - when the firmware is in Secure Boot setup mode, enrolls Cherry's keys
     and reboots, before anything else starts (see [Secure Boot](#secure-boot))

Because `/etc` is read-only, `/root` is a symlink into `/var`.

## Memory

Everything lives in RAM: the root filesystem, `/var` until the machine's
pool has disks (see [Disks](#disks)), and the memory processes allocate.

The root filesystem stays compressed. Its only copy is the EROFS image in the
initramfs: 62 MB for 139 MiB of files before OpenCode, OpenChamber and Bun,
which add about 182 MB for 465 MiB of files (see [OpenCode](#opencode) and
[OpenChamber](#openchamber)), compressed with zstd in clusters of up to 64 KiB.
The kernel keeps the files in use in its
page cache, decompressed, and drops them under memory pressure; it reads them
from the image again when they are needed.

Under memory pressure, the kernel also compresses the coldest pages into zram,
a swap device in RAM, instead of running out:

- **What it compresses:** process memory, and tmpfs pages too (`/tmp`,
  `/run`, `/dev/shm`).
- **`/var` in RAM:** until the pool `cherry` has disks, a second zram device,
  of 132 MiB and zstd too, holds it, so its files cost their compressed size
  (see [Disks](#disks)).
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
- **Hostname:** `cherry-` and 8 hex digits derived from the machine ID, e.g.
  `cherry-f587d723`. The image's `/etc/hostname` is `cherry-????????`
  (`BR2_TARGET_GENERIC_HOSTNAME`), and systemd replaces each `?` with a hex
  digit of a keyed hash of the machine ID as it sets the hostname
  ([hostname(5)](https://www.freedesktop.org/software/systemd/man/latest/hostname.html)).
  A hash rather than the machine ID itself, as the hostname goes out on the
  network, e.g. in DHCP requests.

`/init` leaves the EK and its hash in `/run/cherry/identity/` (`ek.der`,
`ek-hash`) for later boot steps. Both identify the machine but are not secret:
anyone with access to the TPM can read the EK.

**Without a usable TPM 2.0,** boot stops: no TPM, a TPM 1.2, or a TPM that
cannot create its EK. `/init` writes the reason to
`/run/cherry/identity/error` and starts systemd with `cherry-no-tpm.target`
instead of the default target. That target brings up only the basic system and
a login prompt on the console, and shows the error on every console.
Networking, OpenChamber and containers never start.

## Disks

`/var` is the volume `var` of the pool `cherry`, the machine's own storage
pool (see [Storage](#storage)): btrfs, so that an environment is a snapshot
of its image (`machinectl clone`), which takes space only for what it
changes (see [Environments](#environments)). With it persist the containers,
the images, the `cherry` user's state and the journal. The pool's disks are
LUKS2 volumes whose keys the machine's TPM seals to the boot state: the
machine unlocks them at boot without a passphrase, booted into a Cherry
image the PCR key signed; elsewhere, and under anything else, they are
unreadable.

Nothing claims a disk at boot. A machine boots with the pool in RAM: a zram
device of 132 MiB, compressed with zstd, as the pool's one physical volume,
and `var` on it, 128 MiB, the least a btrfs can be (`mkfs.btrfs` wants 109
MiB). That is room for OpenChamber, the OpenCode agent and the journal, not
for a container: nothing persists, and no image fits, until the agent gives
the pool disks. It does that through its storage tools, with the user's yes
to the disks that get erased (`create_pool` named `cherry`, see
[Storage](#storage)): the disks are claimed, the pool is extended with them,
and `var` moves onto them live, a `pvmove` of its 128 MiB, with nothing
stopped or restarted; the zram device then goes (LVM is told to mix its
4 KiB logical blocks with the disks' 512-byte ones: `var` moves from the
larger to the smaller, and its btrfs writes 4 KiB sectors either way). `var`
is then grown (`resize_volume`), and `/var` with it. From then on, `/var`
persists.

`cherry-storage.service` (`cherry-storage boot start`, see
[`cherry-storage`](package/cherry/cherry-storage)) does it before systemd
mounts `/var`, and only on a machine with a usable TPM:

- **Unlock:** every claimed disk, a partition labelled `cherry-pv`, is
  unlocked with the TPM (`systemd-cryptsetup`) as
  `/dev/mapper/cherry-pv-<partition>`, and its key sealed anew if it was
  sealed another way (below). One the TPM cannot unlock (another machine's
  disk, or this one's before its TPM was cleared) is reported and left
  alone.
- **The pool on its disks:** activated (`vgchange`), and `var` mounted on
  `/var` as `/dev/mapper/cherry-var`. A disk of the pool that is missing and
  that no volume uses (the zram device, after a crash between the move of
  `var` off it and its removal) is dropped from the pool; one a volume uses
  fails the unit, and `/var` is a tmpfs for that boot, with no volume
  mounted and the disks as they were (`vgreduce --removemissing --force`, as
  root, gives the volumes up).
- **No pool:** the pool is made in RAM, as above. Disks the TPM cannot
  unlock are not seen, so a machine with another machine's disks boots like
  one without.
- **Without either,** should making the pool in RAM fail, `/var` is a tmpfs
  of up to half the RAM, and nothing persists.

The same unit then mounts the pools' other volumes under `/var` (see
[Storage](#storage)), and systemd fills `/var` with what it lacks of the
image's factory defaults. At shutdown, systemd unmounts the volumes and then
`/var`, and the unit deactivates the pools and closes the disks.

The TPM is the keys' only holder: there is no passphrase or recovery key.
Clearing the TPM (in the firmware setup, or `tpm2_clear`) loses the keys, and
the data with them, though not the machine's identity, which comes from the
endorsement seed.

**Sealed to the boot state:** the TPM unseals a key only while two of its
PCRs hold what they held when the key was sealed:

- **PCR 7,** the firmware's Secure Boot state: whether it is on, and the keys
  it holds (see [Secure Boot](#secure-boot)). Enrolling keys, clearing them
  or turning Secure Boot off changes it, and a disk sealed before stays
  locked after: a machine enrolls Cherry's keys before the agent claims a
  disk, and `cherry-storage.service` waits for `cherry-secureboot.service`,
  which reboots before any key is sealed to a setup-mode boot.
- **PCR 11,** which systemd-stub measures the UKI's sections into, and
  systemd then extends with the boot phases `sysinit` and `ready`, under a
  signed policy: the image carries the PCR key's public half (its `.pcrpkey`
  section) and that key's signatures over the values PCR 11 holds at those
  phases (`.pcrsig`), which systemd-stub puts in the initramfs and
  `cherry-pit` leaves in `/run/systemd/`. The TPM takes a signature by the
  key a disk was sealed under over PCR 11's current value, or unseals
  nothing: only a Cherry image that key signed, as it was built, unlocks the
  disk. A release is signed with the repository's PCR key, so an update
  still unlocks the disks; a build tree signs with a key of its own (see
  [Building](#building)), and a release before the repository has a PCR key
  likewise. The LUKS2 header's token names the key and the PCRs
  (`cryptsetup luksDump --dump-json-metadata`).

A disk whose key was sealed another way (before this policy, or under
another key the image carries signatures for) is sealed to the policy anew as
it is unlocked at boot, and its old slot goes.
[`scripts/pcrsign.py`](scripts/pcrsign.py) computes the expected values,
signs them, and checks an image's policy (`check`), as `systemd-measure`
would. The smoke test proves that the same image with one byte changed, or
signed anew with another key, unlocks nothing.

To start over with an empty `/var`: `release` refuses the pool's disks, which
hold it, so erase the partition and then the disk by hand (`wipefs --all
--force /dev/vda1 /dev/vda`), and reboot. The machine boots with the pool in
RAM, and the disk is blank again, for the agent to claim.

`make qemu` gives the VM no disk. `--disk 8G` attaches
`output/cherry_x86_64/qemu/disk.img`, created blank and sparse at the first
run, which the VM then keeps along with its TPM, for the agent to give to the
pool, sealed under the build tree's key:

```sh
python3 scripts/run_qemu.py --disk 8G
```

## Storage

A machine's disks hold storage pools for the `cherry` user and its agent:
LVM volume groups of the disks it claimed, with volumes in them, mounted
under `/var/lib/cherry/volumes`. The pool `cherry` is the machine's own,
there from boot, and its volume `var` is `/var` (see [Disks](#disks)).
`cherry-storage@.service` ([`/usr/libexec/cherry-storage`](package/cherry/cherry-storage))
does it all as root, for the `cherry` user, which may start it (polkit) but
touches no disk itself; the OpenCode agent gets tools for it (below).

- **Claiming a disk** erases it: `wipefs` on its partitions, then on the
  disk. `systemd-repart` then makes all of it one GPT partition labelled
  `cherry-pv` ([`50-pv.conf`](package/cherry/storage-pv.conf)), a LUKS2 volume
  whose key the TPM seals to the boot state (see [Disks](#disks)):
  `cryptsetup` makes it with a random key, `systemd-cryptenroll` enrolls the
  TPM with that key, sealed to PCR 7 and the signed PCR 11 policy, then
  wipes that key's slot, so the TPM is the only holder of the key
  (systemd-repart's `Encrypt=` would put a filesystem in it). It is unlocked
  as
  `/dev/mapper/cherry-pv-<partition>` and made an LVM physical volume.
  - **As listed:** `list` gives each disk a fingerprint, of its size, serial,
    model and partition table, and a claim names it, so it never takes a disk
    that a reboot renamed, or that changed since.
  - **Not in use:** never a disk that is mounted, swap, or held by device
    mapper or md, nor a removable, read-only, USB or smaller than 1 GiB one.
  - **Another machine's:** a disk with a `cherry-pv` volume this TPM cannot
    unlock can be claimed, which erases it too.
- **LVM sees only those volumes** (Cherry's [`/etc/lvm/lvm.conf`](package/cherry/lvm.conf)):
  LVM on any other disk is never activated, and a disk holding it can be
  claimed, as nothing uses it.
- **The pool `cherry`** is there from boot, in RAM until `extend` gives it
  disks, which moves `/var` onto them (see [Disks](#disks)); a `create` of
  it is refused, as is anything but `extend` while it is in RAM. It is never
  destroyed, `var` never removed, and its last data disk never taken out.
  Otherwise it is a pool like any: more disks, a cache, with `var` cached
  like the rest, volumes beside `var`.
- **A pool** is a volume group of claimed disks:
  - **data disks** hold its volumes, one after the other, with no redundancy;
  - **cache disks**, SSDs, cache every volume (dm-cache, a cache pool): each
    volume is cached on a share of them as large as its share of the data
    disks, writethrough unless the pool is writeback.
- **Volumes are thick, linear LVs,** not thin ones: the image has no
  thin-provisioning-tools, so a thin pool's metadata could be neither checked
  nor repaired. For the same reason, no cache's metadata is checked when it is
  activated, which a writethrough cache can afford: the data disks under it
  hold all the data.
- **A volume** holds btrfs, its root owned by the `cherry` user, mounted
  `nodev,nosuid` on `/var/lib/cherry/volumes/<pool>/<volume>`, which
  `cherry.service` sees writable; `var` is mounted on `/var`. Volumes grow,
  online (`btrfs filesystem resize`), but don't shrink.
- **Taking a data disk out** of a pool moves its data to the pool's other data
  disks first (`pvmove`), with the volumes' caches detached meanwhile.
- **At boot,** before systemd mounts `/var`, `cherry-storage.service`
  (`cherry-storage boot start`) unlocks every `cherry-pv` partition with the
  TPM, sealing anew the key of one sealed another way, brings up the pool
  `cherry` and `/var` (see [Disks](#disks)), then
  activates each other pool, which nothing else does (their volume groups
  are made without autoactivation), and mounts each volume as it is, as
  systemd-fsck leaves btrfs too. At shutdown, once systemd has unmounted the
  volumes and `/var`, it deactivates the pools and closes the disks. A disk
  the TPM cannot unlock, or a volume that will not mount, is reported and
  left alone, and the rest goes on; for the volume, `btrfs check
  /dev/<pool>/<volume>` reports, `btrfs restore` copies files out, and
  `btrfs check --repair` is the last resort.
- **One operation at a time:** another one fails at once. Each logs to
  `/var/log/cherry-storage/<instance>.log`, which the `cherry` user can read.

The instance is a verb and its arguments, separated by colons:

| Instance | Does |
|---|---|
| `list` | the disks, pools and volumes, as JSON in `/var/log/cherry-storage/list.json` |
| `claim:<disk>:<fingerprint>` | erases the disk and makes it Cherry's |
| `release:<disk>:<fingerprint>` | erases a claimed disk that is in no pool, and leaves it blank |
| `create:<pool>:<disk>...` | a pool, of these claimed disks as its data disks (not `cherry`, which is there from boot) |
| `extend:<pool>:<disk>...` | more data disks for the pool; for `cherry` in RAM, its first, which `/var` moves onto |
| `cache:<pool>:<mode>:<disk>...` | the pool's cache disks are now these (none: no cache), `writethrough` or `writeback`; every volume's cache is made anew |
| `reduce:<pool>:<disk>` | takes the disk out of the pool; it stays claimed |
| `destroy:<pool>` | destroys a pool that has no volumes; its disks stay claimed (never `cherry`) |
| `volume-create:<pool>:<volume>:<size>` | a volume of `<n>M`, `G` or `T` (MiB, GiB, TiB), or `all` of the pool's free space |
| `volume-resize:<pool>:<volume>:<size>` | grows the volume (`cherry`/`var`: `/var`) |
| `volume-remove:<pool>:<volume>` | unmounts and removes the volume (never `cherry`/`var`) |

```sh
systemctl start cherry-storage@list && cat /var/log/cherry-storage/list.json
systemctl start cherry-storage@claim:vda:<fingerprint>
systemctl start cherry-storage@extend:cherry:vda          # /var moves onto it
systemctl start cherry-storage@volume-resize:cherry:var:20G
systemctl start cherry-storage@claim:vdb:<fingerprint>
systemctl start cherry-storage@claim:nvme0n1:<fingerprint>
systemctl start cherry-storage@create:tank:vdb
systemctl start cherry-storage@cache:tank:writethrough:nvme0n1
systemctl start cherry-storage@volume-create:tank:data:100G
```

**OpenCode's tools:** `/etc/opencode` gives the agent a second MCP server,
`storage` (`/usr/lib/cherry/storage-mcp.js`, run on Bun), and skills for it:
`storage`, how it all works and the tools' rules; `configure-storage`, to set
a machine's storage up; and `manage-storage`, to change it (grow a pool,
replace a disk, volumes). When asked to configure a machine's storage, the
agent follows `configure-storage`:

1. It lists the disks and asks the user which to claim.
2. It proposes LVM from the chosen disks' kinds, as the kernel reports them:
   - all SSDs, or all hard disks: one pool of all of them;
   - both: one pool of the hard disks, with the SSDs as its cache.

   The pool is `cherry` while that one is in RAM, so the first configuration
   makes `/var` persistent, and sizes `var`.
3. It claims the disks and makes the pool once the user agrees.

The tools, run as the `cherry` user with its privileges alone:

| Tool | Does |
|---|---|
| `list` | the disks (kind, state, fingerprint, what is on them), pools and volumes |
| `claim`, `release` | `claim:` and `release:`, disk by disk |
| `create_pool` | `create:`, then `cache:` for the SSDs given as its cache; named `cherry`, `extend:` instead |
| `add_disks`, `set_cache`, `remove_disk`, `destroy_pool` | `extend:`, `cache:`, `reduce:`, `destroy:` |
| `create_volume`, `resize_volume`, `remove_volume` | `volume-create:`, `volume-resize:`, `volume-remove:` |

Its tools, `storage_*`, are among the few the system configuration allows
the agent (see [Environments](#environments)); it outranks the user's, so
a session cannot take them away or add any other.

Not yet: binding a volume into a container (systemd-nspawn ignores `Bind=` in
an image's own settings, and `machinectl bind` isn't allowed, see
[Security notes](#security-notes)), redundancy (RAID), and recovering a pool
whose cache disk died, which takes `lvconvert --uncache --force` by hand.

`make qemu` storage: `--hdd SIZE` and `--ssd SIZE`, each repeatable, attach
more disks, blank and kept along with the TPM, on virtio-blk, which the VM
sees as hard disks (`vda`, ... after `--disk`), and NVMe, as SSDs
(`nvme0n1`, ...):

```sh
python3 scripts/run_qemu.py --disk 4G --hdd 8G --hdd 8G --ssd 2G
```

## Firewall

`cherry-firewall.service` loads Cherry's nftables rules,
[`/usr/lib/cherry/firewall.nft`](board/x86_64/rootfs-overlay/usr/lib/cherry/firewall.nft),
before networkd configures any link, and they stay until shutdown. Nothing
comes in unasked but ping, to the host or through it to its containers:

- **In:** ping (ICMP and ICMPv6 echo requests), and the replies to what the
  host and its containers send. Besides those, only what the network needs:
  IPv6 neighbor discovery, router advertisements and multicast listener
  queries, and DHCPv6 replies. DHCPv4 needs no rule: networkd's client takes
  its offers on a raw socket, which sees packets before the firewall.
- **Dropped,** silently: everything else.
- **Containers,** on the `ve-*` links of `--network-veth` (which `machinectl
  start` uses) and the `vz-*` bridges of `--network-zone=`, however many:
  - lease their addresses from networkd's DHCP server on the host, ping it,
    and reach out, masqueraded by networkd
  - reach nothing else on the host, and not one another: containers that
    should talk share a zone, whose bridge switches their packets without the
    firewall (the kernel has no bridge netfilter)
  - get nothing from outside unasked, through a port forward (`--port=`)
    either
- **Its own table only,** `inet cherry`: networkd and nspawn keep theirs,
  `io.systemd.nat`, for masquerading and port forwards. Loading the rules
  replaces `inet cherry` alone.

A container bridged onto the host's Ethernet (`--network-bridge=`), or with
`--network-macvlan=` or `--network-ipvlan=`, is on the network itself: its
packets never pass through the host's firewall, and its own network namespace
has rules of its own, none by default.

To let something in until the next boot, add a rule from the console.
Restarting the service puts the image's rules back:

```sh
nft add rule inet cherry input tcp dport 8080 accept    # a port of the host's
nft add rule inet cherry forward ct status dnat accept  # nspawn's port forwards
systemctl restart cherry-firewall
```

## Secure Boot

A [Nitrokey HSM 2](https://docs.nitrokey.com/nitrokeys/hsm/) is the root of
trust of Cherry's UEFI Secure Boot. Three RSA-2048 keys are generated on it
and never leave it:

| Key | Certificate | What it does |
|---|---|---|
| PK | `PK.crt`, Cherry Platform Key | owns the machine's Secure Boot keys: signs changes to the KEK |
| KEK | `KEK.crt`, Cherry Key Exchange Key | signs changes to db and dbx |
| db | `db.crt`, Cherry Secure Boot CA | its certificate is db: the firmware boots what a certificate it issued signs |

A fourth key signs releases in GitHub Actions, which can't reach the Nitrokey:
the CA issues its certificate, `release.crt`, and its private key is a secret
of the `release` environment. A new release key needs no change on any
machine. All four are RSA-2048, the size every firmware verifies.

A fifth, the PCR key, signs the policy that binds the machines' disks to
releases (see [Disks](#disks)): its public half, `pcr.pem`, goes into every
release's image, and its private key is a secret of the `release` environment
too. The Nitrokey has no part in it: the signed image vouches for the key. A
new PCR key leaves every disk sealed under the old one locked, so it is not
replaced lightly.

`board/x86_64/secureboot/` holds the public half: the four certificates, the
signed updates of the PK, KEK and db variables (`PK.auth`, self-signed;
`KEK.auth`, signed by the PK; `db.auth`, signed by the KEK), which the image
enrolls, and `pcr.pem`. `scripts/secureboot.py` writes them; `make check`
verifies them.

**The ceremony,** once, on a computer with the Nitrokey plugged in:

```sh
# pkcs11-tool and sc-hsm-tool (dnf and pacman too)
apt install opensc
# Initialize the Nitrokey, which erases it, with a key share to back up its
# keys with. It ships with the SO PIN 3537363231383830 and the PIN 648219:
# change both afterwards, as Nitrokey's documentation shows.
sc-hsm-tool --initialize --so-pin 3537363231383830 --pin 648219 --dkek-shares 1
sc-hsm-tool --create-dkek-share dkek-share.pbe
sc-hsm-tool --import-dkek-share dkek-share.pbe
# PK, KEK and the CA on the Nitrokey, their files in board/x86_64/secureboot/:
scripts/secureboot.py init
# The release environment, which will hold the release key, open to main and
# the v* tags only (or Settings, Environments, in the repository on GitHub):
gh api -X PUT repos/tiltbob/cherry/environments/release \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api repos/tiltbob/cherry/environments/release/deployment-branch-policies -f name=main -f type=branch
gh api repos/tiltbob/cherry/environments/release/deployment-branch-policies -f name='v*' -f type=tag
# The release key: its certificate in board/x86_64/secureboot/, the key itself
# into the release environment's secret, through memory only, and only when
# issue succeeded (in a pipe, gh would store nothing as the secret).
key=$(scripts/secureboot.py issue --key-out -) && printf '%s\n' "$key" | gh secret set SECUREBOOT_KEY --env release
# The PCR key, likewise: its public half in board/x86_64/secureboot/, the key
# itself into the release environment's secret.
key=$(scripts/secureboot.py pcr --key-out -) && printf '%s\n' "$key" | gh secret set PCR_KEY --env release
git add board/x86_64/secureboot && git commit -m "Secure Boot keys"
```

- Everything on the Nitrokey goes through OpenSC's `pkcs11-tool`: it
  generates the keys and makes their signatures, and the script puts the
  certificates and signed updates together around them.
- The PIN is asked once per command (or taken from `CHERRY_HSM_PIN`). With
  other tokens plugged in, pass `--token` with the Nitrokey's label as
  `pkcs11-tool -L` shows it; `--module` names another PKCS#11 module than
  OpenSC's.
- `init` never reuses a key on the token. Should it stop partway, it removes
  the files it wrote and prints the `pkcs11-tool` commands that delete what
  it generated: run them, then `init` again.
- A wrong PIN counts: the Nitrokey blocks it after three in a row
  (`sc-hsm-tool` shows the tries left), and the SO PIN unblocks it. Both
  commands check the PIN first, and `issue` that the token holds the CA's
  key, before anything else.
- **Back up** the three keys, wrapped by the key share: `sc-hsm-tool
  --wrap-key <file> --key-reference <n>` for each (`pkcs15-tool -D` lists
  their references). A second Nitrokey initialized with the same share takes
  them with `--unwrap-key`. Keep the share and the wrapped keys offline, and
  apart.
- **The release environment** must exist before `gh secret set` can store
  the key in it. Open to `main` and the `v*` tags only, it keeps the key
  from any other branch's runs.
- **A new release key:** run `issue` again and commit the new `release.crt`.
  The old key still signs images that boot until it is revoked (below).
- **A leaked release key:** `scripts/secureboot.py revoke
  board/x86_64/secureboot/release.crt` adds its certificate to `dbx.auth`,
  the signed update that sets dbx to every revoked certificate, which the
  KEK on the Nitrokey signs; then `issue` a new key, and commit both. Every
  release from then on carries `dbx.auth`, and a machine applies it (below):
  the firmware then refuses whatever the revoked key signed. Nothing takes a
  revocation back.
- **The PCR key** is made outside the token, by `pcr`, once: a second `pcr`
  refuses while `pcr.pem` is there. Back the key up as the token's.

**Releases:** the release job of the build workflow signs the release image's
PCR policy with the PCR key (`scripts/pcrsign.py sign`) and checks it
(`check`), then signs `cherry-x86_64.efi` with the release key (`sbsign`),
checks the signature against the CA (`sbverify`), and publishes the signed
image. The release image is the one the smoke test ran, built again with
`pcr.pem` in place of the build's own PCR key and its policy left for the
release job to sign ([`post-image.sh`](board/x86_64/post-image.sh)): that
key is the one difference between what was tested and what ships. Before the
repository has keys, it publishes the image unsigned, and without a PCR key
the tested image as it is, which unlocks only the disks it sealed itself.
Once it has them, a missing secret, or one that isn't `release.crt`'s or
`pcr.pem`'s key, fails the release.

**Enrolling a machine:** clear the firmware's Secure Boot keys, which puts it
in setup mode (the setup menu calls it "Reset to Setup Mode", "Clear Secure
Boot keys" or "Delete all Secure Boot variables"), and HTTP boot a signed
release. Before anything else starts, `cherry-secureboot.service`:

1. finds the drivers the firmware loaded from option ROMs, those of add-in
   network, graphics and storage cards: the firmware measured each into PCR 2
   of the TPM, by the Authenticode SHA-256 that db entries hold, and the TPM's
   event log lists them;
2. sets db to the CA and those hashes, dbx to the revoked release keys when
   the image carries `dbx.auth`, then the KEK, then the PK, which ends setup
   mode. Microsoft signs option ROMs, and db leaves Microsoft's CA out:
   without their hashes, the cards would have no driver. Whatever db and KEK
   held before goes, and dbx too when the image has one;
3. shows what it enrolled on the console, and reboots. The firmware now boots
   only what the CA's certificates signed, and those option ROMs.

It enrolls nothing, and says why on the console, when the TPM's event log is
missing or a driver in it has no SHA-256 (the TPM's SHA-256 PCR bank is off),
or when the firmware refuses a variable before the PK. On every later boot it
reports in the journal (`journalctl -u cherry-secureboot`) whether Secure Boot
is on, with Cherry's keys, and on a machine with Cherry's keys whose dbx is
not the image's `dbx.auth`, sets it: the firmware takes the update, signed by
the KEK and later than what dbx holds, and from the next boot refuses what
the revoked keys signed; one that refuses it is reported on the console, and
the unit fails. When the keys are in but Secure Boot is off, which
is the firmware setup's own switch, out of Cherry's reach, it says so on the
console. Some firmware asks at the next boot to confirm new keys, and drops
them if nobody does; Cherry then reports that instead of enrolling again.

Once a machine has Cherry's keys:

- Only signed releases boot on it with Secure Boot on: not a local build,
  nor other systems (shim, Windows) that Microsoft's CA signed.
- A new add-in card, or a card's firmware update, brings an option ROM db
  doesn't know: clear the keys and boot Cherry again, to enroll anew.
- systemd-stub ignores command-line overrides passed by the firmware.

**The kernel is locked down,** in integrity mode, from early boot and however
the machine booted (`/sys/kernel/security/lockdown`): Secure Boot keeps the
firmware from starting anything but a signed release, and lockdown keeps what
it started as it was built. The kernel loads no module the build did not
sign: every module the image carries is signed at build time, with a key the
kernel build makes and keeps in the build tree and whose certificate the
kernel holds, and `insmod` of anything else fails with "Key was rejected by
service". Locked down too: `/dev/mem`, kexec of unsigned images,
hibernation, raw PCI, port and MSR access, debugfs, and unsafe module
parameters. The smoke test checks that every module the image carries is
signed, loads one, and has a copy of it without its signature refused.

`python3 scripts/run_qemu.py --secure-boot` boots OVMF's Secure Boot build,
whose variable store `output/cherry_x86_64/qemu/OVMF_VARS.secboot.fd` starts
in setup mode and persists: the first run enrolls the keys and QEMU exits as
the VM reboots; the next one then refuses a local build, which is unsigned
unless the build has a local chain (below). The smoke test's Secure Boot
scenario goes through it all, and boots the image signed by a CA of its own,
which it adds to db, with Secure Boot on, or as it is when the build signed
it. Without keys in `board/x86_64/secureboot/`, the image enrolls nothing and
the scenario is skipped.

**A local chain** has local builds boot with Secure Boot on, on test machines
and VMs that enrolled it, the way releases do on machines with Cherry's keys.
The image carries the keys of `BR2_PACKAGE_CHERRY_SECUREBOOT_KEYS`, the
repository's directory by default. Make a hierarchy of your own with
`scripts/secureboot.py init` and `issue` on a SoftHSM token, as
`tests/ceremony.sh` does (`apt install softhsm2 opensc sbsigntool`), into a
directory of its own, with the release key beside its certificate:

```sh
softhsm2-util --init-token --free --label local --so-pin 12345678 --pin 123456
module=/usr/lib/softhsm/libsofthsm2.so   # where the distribution puts it
scripts/secureboot.py --dir ~/cherry-chain init --module $module --token local
scripts/secureboot.py --dir ~/cherry-chain issue --module $module --token local --key-out ~/cherry-chain/release.key
```

Point `BR2_PACKAGE_CHERRY_SECUREBOOT_KEYS` at that directory (`make
menuconfig`, leaving the defconfig as it is) and build: the image carries
that chain's `.auth` files, and `post-image.sh` signs it with `release.key`
(`sbsign`), as the release job signs a release with its key. A machine in
setup mode enrolls the local chain and boots the signed local build with
Secure Boot on, and `make test` boots it as it is. The repository's
directory never holds a `release.key`: releases are signed in CI, from the
`release` environment's secret, and CI's builds stay the ones for
distribution.

Not yet: an older release still boots, and unlocks the disks a newer one
sealed, as long as its release key is not revoked, and revoking the key
refuses every release it signed; dbx holds no hashes of single releases.

## Running containers

Put container trees in `/var/lib/machines`, then start them with
`machinectl start <name>` or run `systemd-nspawn` directly.

With `--network-veth` (or `VirtualEthernet=yes`), systemd-networkd serves DHCP
to the container and masquerades its traffic. The firewall lets the container
out, but nothing in unasked (see [Firewall](#firewall)).

Once the machine's pool has disks (see [Disks](#disks)), containers and
their configuration persist. Before that, `/var` is 128 MiB in RAM: no
container fits, and nothing lasts past a reboot.

### As the cherry user

The `cherry` user, which [cherry.service](#cherry-service) runs OpenChamber and
the OpenCode agent as, manages containers too, without being root. polkit
rules (`/usr/share/polkit-1/rules.d/50-cherry.rules`) let it drive
systemd-machined with `machinectl`: `start`, `poweroff`, `reboot`,
`terminate`, `kill`, `remove`, `clone`, `rename`, `read-only`, `set-limit`,
`shell` and `login` on any container, though not the host's own shell or login
(`machinectl shell .host`), nor `copy-to`, `copy-from` or `bind`, which
machined does as root on any host path (see [Security notes](#security-notes)),
or `clean` and `set-limit` on all the images at once. `machinectl start` is a
start of `systemd-nspawn@<name>.service`, so it may start, stop and restart
those units, and no other but `cherry-bootstrap@.service`, which creates a
container: it runs the bootstrap tool below as root, with the README's command
for it, `cherry-image@.service`, which builds images (see
[Building images](#building-images)), and `cherry-storage@.service`, which
claims disks and makes pools and volumes of them (see [Storage](#storage)).
The instance of `cherry-bootstrap@` is
the distribution, its release and the machine's name, separated by colons:

```sh
systemctl start cherry-bootstrap@debian:trixie:mybox    # debootstrap
systemctl start cherry-bootstrap@ubuntu:noble:mybox     # debootstrap, with Ubuntu's keyring and archive
systemctl start cherry-bootstrap@fedora:44:mybox        # rpmstrap, any of its profiles
systemctl start cherry-bootstrap@arch::mybox            # pacstrap
machinectl start mybox
machinectl shell mybox
```

`systemctl start` returns when the container is ready, or fails; the tool's
output is in `/var/log/cherry-bootstrap/<instance>.log`, readable by the
`cherry` user, and a failed bootstrap leaves no tree behind.

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
- Ubuntu needs its own keyring and archive, both on the image:
  `--keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg noble <dir>
  http://archive.ubuntu.com/ubuntu`, as `cherry-bootstrap@ubuntu:noble:<name>`
  runs it. Other derivatives need theirs.

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

### Building images

`cherry-image@.service` builds container images to measure, from a recipe:
for the `cherry` user, and so for the OpenCode agent, which gets tools for it
(below). Root can use it too. Images never leave the machine that built them:
nothing writes one out for another machine, which builds its own from a
recipe. A recipe is a directory, `/var/lib/cherry/images/<name>/`:

| File | Holds |
|---|---|
| `image.conf` | `Distribution=` and `Release=`, as `cherry-bootstrap@` takes them, and `Packages=`, more packages for the image's own apt-get, dnf or pacman |
| `extra/` | a tree copied onto the image's root, owned by root |
| `postinst` | a script run last in the image, as its root, with network |
| `image.nspawn` | the image's settings, installed as `/var/lib/machines/<name>.nspawn` |

The instance is a verb and the image's name, separated by a colon:

```sh
systemctl start cherry-image@build:web   # /var/lib/machines/web, from /var/lib/cherry/images/web
machinectl start web
machinectl poweroff web
```

- **build:** bootstraps the distribution with `cherry-bootstrap` under
  `/var/lib/machines/.cherry-image/`, then does the rest in the image, under
  `systemd-nspawn` with a user namespace, so that the recipe's script is root
  only in there: drops the host's `/etc/hostname`, which debootstrap copies,
  so that each container takes its machine's name unless the recipe sets one;
  installs the packages, copies `extra/` (into the image's own directories,
  links such as `/bin -> usr/bin` included), runs `postinst`, empties
  `/etc/machine-id`, so that each container of the image gets its own, and
  clears the package caches. Then the image takes its name. A failed build
  leaves nothing behind, and a build never replaces an image: `machinectl
  remove` it first. Image names are letters, digits and `-`, up to 48.
- **As the cherry user:** the recipe is read by processes that run as
  `cherry` (`systemd-run`), so the builder, which runs as root, reads only
  what that user could. Settings come only from a regular file of the recipe,
  and for images in `/var/lib/machines` systemd-nspawn ignores the privileged
  ones anyway (`Bind=`, `Capability=`, the network settings, `Port=`).
- **Log:** `/var/log/cherry-image/<instance>.log`, readable by the `cherry`
  user. A failed unit is not kept among the failed units, which that user
  could not reset: the log is the record.
- **Space:** images live on `/var`, the machine's disk or RAM.

### Environments

Two kinds of thing live in `/var/lib/machines`, both the `cherry` user's to
manage:

- An **image** is built from a **profile**: a recipe as above, written by a
  person, plus `profile.conf` with a `Title=` and a `Description=`. The image
  ships profiles in `/usr/lib/cherry/profiles/`, one per distribution it can
  bootstrap (`debian-trixie`, `ubuntu-noble`, `fedora-44`, `arch`,
  `almalinux-10`, `rocky-10`, `centos-stream-10`), each booting with its veth
  link configured; the machine's administrator adds their own under
  `/var/lib/cherry/profiles/`, which win over the image's by id, and persist
  with `/var`. Building copies the profile to `/var/lib/cherry/images/<name>/`,
  notes the profile's id in its `profile.conf`, and runs
  `cherry-image@build:<name>`; that recipe is what marks the machine as an
  image. An image is never started: it is what environments are made from.
- An **environment** is a clone of an image (`machinectl clone`, with the
  image's `.nspawn` settings: a snapshot of the image's subvolume on `/var`'s
  btrfs, which takes space only for what it changes), started, stopped and
  removed on its own, with changes of its own; several can come from one
  image.
  `/var/lib/cherry/environments/<name>/environment.conf` records its image,
  and is what marks the machine as an environment. An image with environments
  is neither removed nor rebuilt until they are gone.

A machine with neither, made by hand with the bootstrap tools, is "other":
it can be cloned, started and stopped, not built. That is the whole
interface: a profile and a name make an image, an image and a name make an
environment. No package list, file or script comes from the person or the
agent asking; a new need is a new profile.

The verbs are one module, `/usr/lib/cherry/environments.js`, behind two
front ends that run as the `cherry` user with its privileges alone:

- **OpenChamber's Environments extension** (see [OpenChamber](#openchamber)):
  a panel in the rail with the environments (create from an image, or from a
  profile whose image is built first when there is none; start, stop,
  remove, status) and the images (build from a profile, the build log,
  remove); the `/env` command in the chat (`/env create ubuntu-noble yarp`,
  `/env status yarp`, `/env build debian-trixie`); and a box that reads a
  sentence ("create a new ubuntu environment named yarp") into one of those,
  shown back before it runs. No model is involved: a small parser does it,
  and asks when a word is missing. The panel talks to a service OpenChamber
  starts on loopback inside `cherry.service`, which holds the verbs and
  follows builds, clones, starts and stops as jobs.
- **OpenCode's MCP server**, `machines` (`/usr/lib/cherry/machines-mcp.js`,
  run on Bun), named by `/etc/opencode/opencode.json`, OpenCode's system
  configuration, with a skill, `machine-images`, for the workflow. Its tools:

| Tool | Does |
|---|---|
| `list` | the images (with the profile each came from and its environments), the environments (with their image, and whether they run), the other machines, and the profiles |
| `build` | an image from a profile: copies it to the recipe and runs `cherry-image@build:<name>`; with `replace`, `machinectl remove` first, if no environments remain |
| `remove_image` | `machinectl remove` of an image with no environments left; its recipe stays |
| `log` | the end of an image's build log |
| `create` | an environment: `machinectl clone` of an image by name, or of the image of a profile, named after it and built first when there is none |
| `start`, `stop` | `machinectl start`, not of an image, waiting until the container lets a shell in; `poweroff`, then `terminate` if it is still up |
| `status` | the kind, image or profile, and through `machinectl shell` a fixed script: systemd's state, the address of `host0`, the failed units |
| `remove` | `machinectl remove` of an environment, with its settings and its record |

The same configuration denies the agent every other tool (`permission`:
`*` is `deny`; `machines_*`, `storage_*`, `skill` and `question` are
allowed), so the agent on the host manages environments and storage (see
[Storage](#storage)) and nothing else: no shell, no files, no web. The
system configuration outranks the user's, so neither the servers nor the
lockdown can be undone from a session.

## OpenCode

Every Cherry machine has [OpenCode](https://opencode.ai), the AI coding agent,
as `/usr/bin/opencode`: its server (`opencode serve`, the HTTP API and web UI)
and its clients. The package can also run the server as a system service, with
`BR2_PACKAGE_OPENCODE_SERVICE`: the `opencode` user, `/var/lib/opencode` and
`opencode.service`, on 127.0.0.1:4096 with a password for the boot in
`/var/lib/opencode/server-password`. Cherry leaves that off: OpenChamber runs
the server, as the `cherry` user (see [Cherry service](#cherry-service)).

- **Binary:** upstream's prebuilt x86_64 build, from the npm platform package
  `@opencode/cli-linux-x64-baseline`: the "baseline" variant that doesn't need
  AVX2, so it runs on VM CPU models without it. It is a Bun single-file
  executable, which stripping breaks: Buildroot's strip skips it
  (`BR2_STRIP_EXCLUDE_FILES`) and `post-build.sh` checks that. It is 202 MiB,
  about 98 MB in the image, and the idle server uses about 170 MB of RAM.
  Automatic updates are off: a new version comes with a new image.
- **Tools:** on Cherry, the agent has the `cherry` package's MCP servers,
  for environments and for storage, and nothing else: its own tools are
  denied in `/etc/opencode` (see [Environments](#environments) and
  [Storage](#storage)). bash, the image's ripgrep and git stay on the image
  for OpenChamber's terminals and git panel, and for OpenCode elsewhere.
- **Providers:** none are configured; nothing on the machine holds a key.
  Connect one at runtime, in OpenChamber. The credential lands under
  `/var/lib/cherry`: on the machine's pool once it has disks, and in RAM
  before, gone at the next boot (see [Disks](#disks)).

## OpenChamber

[OpenChamber](https://github.com/openchamber/openchamber) is a web workspace
for running and reviewing the agent's work: its server runs OpenCode and
serves the browser UI.

- **Runtime:** upstream's npm package `@openchamber/web` with its
  dependencies, under `/usr/lib/openchamber`, run on [Bun](https://bun.sh)
  (`/usr/bin/bun`, upstream's prebuilt x86_64 "baseline" build), as upstream's
  own container does. `openchamber` on the command line is its CLI.
- **Service:** the package can run the server as a system service, with
  `BR2_PACKAGE_OPENCHAMBER_SERVICE`: the `openchamber` user,
  `/var/lib/openchamber` and `openchamber.service`. Cherry leaves that off and
  runs it as the `cherry` user instead (see [Cherry service](#cherry-service)).
  `/usr/libexec/openchamber-serve` is the launcher for either unit:
  OpenChamber's server in the foreground on port 3000, with no password on the
  browser UI.
- **Left out:** local speech recognition and synthesis (sherpa-onnx, 32 MiB,
  plus models downloaded at run time), and tunnels (no cloudflared or ngrok on
  the image). Update checks are off and `openchamber update` says so, as the
  image is read-only; the device-pairing relay reaches the network.
- **Environments:** Cherry's extension for its container environments, a
  built-in of the package (`server/built-in-extensions/cherry-environments`,
  named by the registry there), so it is installed and granted on every boot
  with nothing to click: Settings, Extensions shows it as bundled, to pause
  but not remove. Its service runs as the `cherry` user inside
  `cherry.service`; OpenChamber's grant card says such a service has the
  user's access, which the unit confines (see
  [Environments](#environments)). Extensions load in the web UI and the
  desktop app, over the relay too; the mobile app and VS Code do not load
  them.
- **Size:** Bun is 85 MiB, about 42 MB in the image; the npm tree 178 MiB,
  about 42 MB. The idle server uses about 115 MB of RAM.

## Cherry service

`cherry.service`, from the `cherry` package, runs OpenChamber's server as the
`cherry` user, with `/var/lib/cherry` as its home. It starts only on a
machine with a TPM.

- **User and state:** everything runs as `cherry`: OpenChamber, the OpenCode
  it starts, and the agent's tools and terminals. Their data is under
  `/var/lib/cherry` (OpenChamber's in `.config/openchamber`), and the agent
  works in `/var/lib/cherry/work` unless a project picks another directory.
  All of it persists once the machine's pool has disks, and is new at every
  boot before (see [Disks](#disks)); the volumes of its storage pools are
  mounted under `/var/lib/cherry/volumes` (see [Storage](#storage)).
  `/var/lib/cherry` is the only place the service can
  write: it sees the rest of the system read-only, with a private `/tmp`, and
  runs without capabilities, under a system call filter (systemd's
  `@system-service` set, less `@privileged`, whose calls fail with `EPERM`).
- **Ports:** loopback only, both: OpenChamber on 127.0.0.1:3000, and the
  OpenCode it runs on 127.0.0.1 and a free port. Neither can listen on a Unix
  socket. Reach the UI from another device, through the relay of a pairing
  link (below): the firewall lets nothing in, and there is no SSH.
  `/health` reports OpenChamber's version, `/api/opencode/compatibility` the
  OpenCode it runs.
- **Password:** none, on the browser UI or its API (see
  [Security notes](#security-notes)).
- **Restart:** always, after a crash as after a clean exit, such as
  `openchamber stop` or `restart` from a shell.
- **Pairing link on the console:** once the server is online,
  `cherry-connect-url.service` shows the link that pairs another OpenChamber
  app with it, and its QR code, on every console, before the login prompts,
  and in the journal: `openchamber connect-url --relay --qr`, run as the
  `cherry` user. The link carries the relay transport, so a device off the
  local network can use it; it is single-use and expires after 10 minutes.
  `systemctl restart cherry-connect-url.service` shows a fresh one.

To use it, open the link, or scan the QR code, in another OpenChamber app:
the desktop or mobile app, or a browser's. That is the whole way in, on the
prod image as on the dev image; the dev image's root on the console is the
other.

## Security notes

The dev image is for development, and releases are the prod image (see
[Building](#building)):
- On the dev image, `root` has no password on the console (Buildroot's
  default). On the prod image, root is locked out: nothing logs in as root,
  on the console or otherwise, and nothing on the machine can set a password,
  as `/etc` is read-only. A prod machine is managed through OpenChamber
  alone, as the `cherry` user.
- There is no SSH on either image, and the firewall lets nothing in unasked
  but ping (see [Firewall](#firewall)).
- OpenChamber's browser UI has no password: any local user can use it, and so
  run commands as the `cherry` user. It is on loopback only, as is the
  OpenCode behind it. The device-pairing relay forwards its requests from
  loopback too, so OpenChamber takes them as a local user's: whatever the
  relay lets through has the same access.
- The OpenCode agent on the host has no tool but the environment and storage
  verbs (see [Environments](#environments) and [Storage](#storage)): it
  cannot run a command, read or write a file, or reach the web, and nothing
  it is asked turns into a package list or a script. OpenChamber's terminals
  are the human's, and still run as the `cherry` user.
- The `cherry` user creates, starts, enters and removes containers through
  polkit (see [As the cherry user](#as-the-cherry-user)): root inside any
  container, and the bootstrap tools run as root on the host for it, with
  checked arguments. So do image builds (see
  [Building images](#building-images)), whose package installs and scripts
  run as root in the image, in a user namespace. It may not copy files
  between a container and the host or bind-mount host directories into one
  (`machinectl copy-to`, `copy-from`, `bind`): systemd-machined does those as
  root, on host paths the caller picks, so they would give it the host (any
  file to read, root's `/var/roothome/.profile` to write).
- The `cherry` user claims disks through polkit (see [Storage](#storage)),
  which erases them: any disk nothing uses, as `cherry-storage` runs as root
  for it, with checked arguments. A process running as that user, the agent
  or a terminal of OpenChamber's, can erase such a disk, or a pool's volume,
  though not `/var`'s pool or volume.
- The TPM unseals the keys of the claimed disks, `/var`'s pool among them,
  only booted into a Cherry image the PCR key signed, with Secure Boot as it
  was when they were sealed (see [Disks](#disks)): nothing else that boots
  the machine (an HTTP boot server, a USB stick) can unlock them. Any
  release signed with that key does, older ones with their holes included,
  until their release key is revoked, which takes every release it signed
  (see [Secure Boot](#secure-boot)); and `root` on the dev image's console
  then has the disks open. A build tree's images unlock only the disks they
  sealed themselves.
- Releases are signed for Secure Boot, and a machine in setup mode enrolls
  Cherry's keys (see [Secure Boot](#secure-boot)). Local builds are not
  signed, unless the build has a local chain, which only machines that
  enrolled that chain trust. The kernel is locked down, in integrity mode,
  and loads only the modules the build signed, however the machine booted.
