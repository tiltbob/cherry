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
- **`/var`:** the machine's encrypted disk, unlocked by the TPM at boot,
  where containers persist. A machine without one gets one on its first
  blank disk; one without a blank disk keeps `/var` in a tmpfs of up to half
  the RAM, so nothing persists (see [Disks](#disks)).
- **Swap:** compressed, in RAM (zram; see [Memory](#memory)).
- **Firewall:** nothing comes in unasked but ping, to the host or to its
  containers (see [Firewall](#firewall)).
- **Services:** systemd 258 with networkd (DHCP), resolved, timesyncd,
  journald, machined/machinectl and systemd-nspawn, plus OpenSSH and
  OpenChamber running the OpenCode agent, as the `cherry` user (see
  [Cherry service](#cherry-service)).

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
| `make check` | fast checks that need no build (below) |
| `make clean` | remove `output/cherry_x86_64` |

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
argument checks and tool calls, `post-build.sh`'s check of `opencode`, the
firewall's rules, in network namespaces standing in for a host, its LAN and
its containers, the Secure Boot keys in `board/x86_64/secureboot/` and what
`cherry-secureboot` enrolls from them, the key ceremony on a SoftHSM token,
and `cherry-var.service`'s choice of disk. `make -k check` runs on after a
failure; `make check-<name>` runs one. They need shellcheck, flake8,
python3-magic, python3-yaml, systemd's `systemd-analyze` and openssl; node for
the polkit rules, nft, ip, busybox and user namespaces for the firewall, and
softhsm2, opensc and pkcs11-provider for the ceremony (each skipped without
them).

`scripts/update_packages.py` bumps Cherry's packages to their latest upstream
releases, hashes included: it looks up GitHub and GitLab tags and releases,
the npm registry and Debian's snapshot archive, and has Buildroot download the
new release to hash it. `--check` only reports what is newer; a
`package=version` argument picks a version. It needs git, and npm for
OpenChamber. Then build, run `make test`, and update the sizes below.

## Booting

The image is `output/cherry_x86_64/images/cherry-x86_64.efi` from a build, or
the asset of a [GitHub release](https://github.com/tiltbob/cherry/releases):
CI builds every published release from its tag and, once the smoke test
passed, signs `cherry-x86_64.efi` for Secure Boot (see
[Secure Boot](#secure-boot)) and attaches it and its SHA-256. To cut a release
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
- for the smoke test's Secure Boot scenario, `sbsign` (`apt install
  sbsigntool`) and openssl

The TPM is attached through CRB, as firmware TPMs are. Pass `--tpm tis` to
`scripts/run_qemu.py` for the TIS interface of discrete TPMs, or `--tpm none`
to see Cherry stop without a TPM. `--secure-boot` boots OVMF's Secure Boot
build instead, in setup mode at first (see [Secure Boot](#secure-boot)).

`root` logs in without a password on the console. SSH is forwarded from host
port 2222, but the firewall drops it (see [Firewall](#firewall)). To log in
over SSH, pass a key as a [systemd credential](https://systemd.io/CREDENTIALS/),
then let SSH in from the console:

```sh
python3 scripts/run_qemu.py --credential "ssh.authorized_keys.root=$(cat ~/.ssh/id_ed25519.pub)"
nft add rule inet cherry input tcp dport 22 accept    # on the console
ssh -p 2222 root@localhost
```

## How it starts

1. **Firmware:** downloads and starts the UKI. systemd-stub then boots the
   kernel with the embedded command line and initramfs.
2. **Kernel:** unpacks the initramfs into a ramfs and runs its `/init`,
   [cherry-pit](package/cherry-pit/src/cherry-pit.c). The initramfs holds
   only cherry-pit and the root filesystem image. (`rootfstype=ramfs`: EROFS
   can mount a file from a ramfs, but not from the default tmpfs.)
3. **cherry-pit:** mounts the image read-only, straight from the file, makes it
   the root, and runs its [`/init`](board/x86_64/rootfs-overlay/init), a shell
   script.
4. **`/init`:** does what an initrd would before systemd starts:
   - mounts `/dev`, `/proc`, `/sys` and `/run`
   - derives the machine's identity from the TPM (see
     [Machine identity](#machine-identity))
   - starts systemd with that machine ID, or, without a usable TPM, with
     `cherry-no-tpm.target` instead of the default target
5. **systemd:**
   - mounts `/var`: the machine's encrypted disk, which the TPM unlocks, or
     else a tmpfs (see [Disks](#disks))
   - fills `/var` with what it lacks of the image's factory defaults
     (Buildroot's `BR2_INIT_SYSTEMD_VAR_FACTORY`)
   - sets up compressed swap in RAM (see [Memory](#memory))
   - loads the firewall before networkd configures any link (see
     [Firewall](#firewall))
   - when the firmware is in Secure Boot setup mode, enrolls Cherry's keys
     and reboots, before anything else starts (see [Secure Boot](#secure-boot))

Because `/etc` is read-only:
- SSH host keys are generated under `/var/lib/sshd`, so they change on every
  boot unless `/var` is on a disk
- `/root` is a symlink into `/var`

## Memory

Everything lives in RAM: the root filesystem, `/var` with its containers
unless the machine has a disk for it (see [Disks](#disks)), and the memory
processes allocate.

The root filesystem stays compressed. Its only copy is the EROFS image in the
initramfs: 62 MB for 139 MiB of files before OpenCode, OpenChamber and Bun,
which add about 182 MB for 465 MiB of files (see [OpenCode](#opencode) and
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
Networking, sshd and containers never start.

## Disks

A machine keeps `/var` on an encrypted disk of its own, and with it the
containers, the SSH host keys and the `cherry` user's state. That disk is a
GPT partition labelled `cherry-var`: a LUKS2 volume holding ext4, whose key
the machine's TPM seals. The machine unlocks it at boot without a
passphrase; elsewhere, it is unreadable.

`cherry-var.service` ([`/usr/lib/cherry/var`](board/x86_64/rootfs-overlay/usr/lib/cherry/var))
sets it up before systemd mounts `/var`, and only on a machine with a usable
TPM:

- **Unlock:** given a partition labelled `cherry-var`, it unlocks it with the
  TPM (`systemd-cryptsetup`) as `/dev/mapper/cherry-var`, checks its
  filesystem (`e2fsck -p`) and mounts it on `/var`.
- **First boot:** without one, it claims the first blank disk, in the order
  of the kernel's names (`nvme0n1`, `sda`, `vda`, ...). `systemd-repart`
  writes a GPT and makes all of the disk the `cherry-var` partition, with a
  new key that the TPM seals
  ([`repart.d`](board/x86_64/rootfs-overlay/usr/lib/cherry/repart.d)).
  systemd then fills the new `/var` from the image's factory defaults.
- **Blank:** a whole disk of at least 1 GiB on which `wipefs` finds no
  signature: no partition table, filesystem, or RAID or volume manager
  member. Never a removable, read-only or USB disk. `wipefs -a /dev/<disk>`
  makes a disk blank.
- **Without either,** `/var` is a tmpfs, so nothing persists.

Nothing erases a disk:

- If the `cherry-var` partition can't be unlocked (another machine's disk,
  or a cleared TPM) or its filesystem can't be repaired,
  `cherry-var.service` fails and `/var` is a tmpfs for that boot. The disk
  stays as it was.
- With more than one `cherry-var` partition, it uses none, and fails.
- A machine with a `cherry-var` partition never claims a blank disk, even
  if it can't unlock that partition.

To repair a filesystem that `e2fsck -p` left with errors, unlock it by hand
(`systemd-cryptsetup attach cherry-var /dev/vda1 - tpm2-device=auto`), run
`e2fsck /dev/mapper/cherry-var`, then
`systemd-cryptsetup detach cherry-var`, and reboot.

To start over with an empty `/var`, erase the `cherry-var` partition, then
its disk (`wipefs --all --force /dev/vda1 /dev/vda`), and reboot: the disk is
blank again, so the next boot makes a new volume, with a new key.

The TPM is the key's only holder: there is no passphrase or recovery key.
Clearing the TPM (in the firmware setup, or `tpm2_clear`) loses the key, and
the data with it, though not the machine's identity, which comes from the
endorsement seed. The key isn't bound to the boot state yet (see
[Security notes](#security-notes)).

`make qemu` gives the VM no disk. `--disk 8G` attaches
`output/cherry_x86_64/qemu/disk.img`, created blank and sparse at the first
run, which the VM then keeps along with its TPM:

```sh
python3 scripts/run_qemu.py --disk 8G
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
- **Dropped,** silently: everything else, SSH included.
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
nft add rule inet cherry input tcp dport 22 accept      # SSH to the host
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

`board/x86_64/secureboot/` holds the public half: the four certificates, and
the signed updates of the PK, KEK and db variables (`PK.auth`, self-signed;
`KEK.auth`, signed by the PK; `db.auth`, signed by the KEK), which the image
enrolls. `scripts/secureboot.py` writes them; `make check` verifies them.

**The ceremony,** once, on a computer with the Nitrokey plugged in:

```sh
# pkcs11-tool, sc-hsm-tool, and OpenSSL's PKCS#11 provider (dnf and pacman too)
apt install opensc pkcs11-provider
# Initialize the Nitrokey, which erases it, with a key share to back up its
# keys with. It ships with the SO PIN 3537363231383830 and the PIN 648219:
# change both afterwards, as Nitrokey's documentation shows.
sc-hsm-tool --initialize --so-pin 3537363231383830 --pin 648219 --dkek-shares 1
sc-hsm-tool --create-dkek-share dkek-share.pbe
sc-hsm-tool --import-dkek-share dkek-share.pbe
# PK, KEK and the CA on the Nitrokey, their files in board/x86_64/secureboot/:
scripts/secureboot.py init
# The release key: its certificate in board/x86_64/secureboot/, the key itself
# straight into the release environment's secret.
scripts/secureboot.py issue --key-out - | gh secret set SECUREBOOT_KEY --env release
git add board/x86_64/secureboot && git commit -m "Secure Boot keys"
```

- The PIN is asked once per command (or taken from `CHERRY_HSM_PIN`). With
  other tokens plugged in, pass `--token` with the Nitrokey's label as
  `pkcs11-tool -L` shows it; `--module` names another PKCS#11 module than
  OpenSC's.
- `init` never reuses a key on the token. Should it stop partway, it removes
  the files it wrote and prints the `pkcs11-tool` commands that delete what
  it generated: run them, then `init` again.
- **Back up** the three keys, wrapped by the key share: `sc-hsm-tool
  --wrap-key <file> --key-reference <n>` for each (`pkcs15-tool -D` lists
  their references). A second Nitrokey initialized with the same share takes
  them with `--unwrap-key`. Keep the share and the wrapped keys offline, and
  apart.
- **The release environment** (Settings, Environments): limit its deployment
  branches and tags to `main` and `v*`, so that only release runs see the key.
- **A new release key:** run `issue` again and commit the new `release.crt`.
  The old key still signs images that boot until it is revoked (below).

**Releases:** the release job of the build workflow signs `cherry-x86_64.efi`
with the release key (`sbsign`), checks the signature against the CA
(`sbverify`), and publishes the signed image. Before the repository has keys,
it publishes the image unsigned. Once it has, a missing secret, or one that
isn't `release.crt`'s key, fails the release.

**Enrolling a machine:** clear the firmware's Secure Boot keys, which puts it
in setup mode (the setup menu calls it "Reset to Setup Mode", "Clear Secure
Boot keys" or "Delete all Secure Boot variables"), and HTTP boot a signed
release. Before anything else starts, `cherry-secureboot.service`:

1. finds the drivers the firmware loaded from option ROMs, those of add-in
   network, graphics and storage cards: the firmware measured each into PCR 2
   of the TPM, by the Authenticode SHA-256 that db entries hold, and the TPM's
   event log lists them;
2. sets db to the CA and those hashes, then the KEK, then the PK, which ends
   setup mode. Microsoft signs option ROMs, and db leaves Microsoft's CA out:
   without their hashes, the cards would have no driver. Whatever db and KEK
   held before goes;
3. shows what it enrolled on the console, and reboots. The firmware now boots
   only what the CA's certificates signed, and those option ROMs.

It enrolls nothing, and says why on the console, when the TPM's event log is
missing or a driver in it has no SHA-256 (the TPM's SHA-256 PCR bank is off),
or when the firmware refuses a variable before the PK. On every later boot it
reports in the journal (`journalctl -u cherry-secureboot`) whether Secure Boot
is on, with Cherry's keys. When the keys are in but Secure Boot is off, which
is the firmware setup's own switch, out of Cherry's reach, it says so on the
console. Some firmware asks at the next boot to confirm new keys, and drops
them if nobody does; Cherry then reports that instead of enrolling again.

Once a machine has Cherry's keys:

- Only signed releases boot on it with Secure Boot on: not a local build,
  nor other systems (shim, Windows) that Microsoft's CA signed.
- A new add-in card, or a card's firmware update, brings an option ROM db
  doesn't know: clear the keys and boot Cherry again, to enroll anew.
- systemd-stub ignores command-line overrides passed by the firmware.

`python3 scripts/run_qemu.py --secure-boot` boots OVMF's Secure Boot build,
whose variable store `output/cherry_x86_64/qemu/OVMF_VARS.secboot.fd` starts
in setup mode and persists: the first run enrolls the keys and QEMU exits as
the VM reboots; the next one then refuses a local build, which is unsigned.
The smoke test's last scenario goes through it all, and boots the image signed
by a CA of its own, which it adds to db, with Secure Boot on. Without keys in
`board/x86_64/secureboot/`, the image enrolls nothing and the scenario is
skipped. The image carries the keys from `BR2_PACKAGE_CHERRY_SECUREBOOT_KEYS`:
to try a key hierarchy of your own, e.g. made by `scripts/secureboot.py` on a
SoftHSM token (as `tests/ceremony.sh` does), point it there, run `make
br-cherry-secureboot-reinstall all`, and sign the image with `sbsign`.

Not yet: Cherry applies no dbx updates, which would revoke a leaked release
key (the KEK would sign them); the kernel's lockdown and module signing are
off; and nothing is bound to the PCRs that record Secure Boot's state.

## Running containers

Put container trees in `/var/lib/machines`, then start them with
`machinectl start <name>` or run `systemd-nspawn` directly.

With `--network-veth` (or `VirtualEthernet=yes`), systemd-networkd serves DHCP
to the container and masquerades its traffic. The firewall lets the container
out, but nothing in unasked (see [Firewall](#firewall)).

On a machine with its encrypted disk (see [Disks](#disks)), containers and
their configuration persist. Without one, they live in memory and are gone
after a reboot.

### As the cherry user

The `cherry` user, which [cherry.service](#cherry-service) runs OpenChamber and
the OpenCode agent as, manages containers too, without being root. polkit
rules (`/usr/share/polkit-1/rules.d/50-cherry.rules`) let it drive
systemd-machined with `machinectl`: `start`, `poweroff`, `terminate`, `kill`,
`remove`, `clone`, `shell` and `login` on any container, though not the host's
own shell or login (`machinectl shell .host`). `machinectl start` is a start
of `systemd-nspawn@<name>.service`, so it may start, stop and restart those
units, and no other but `cherry-bootstrap@.service`, which creates a
container: it runs the bootstrap tool below as root, with the README's command
for it. The instance is the distribution, its release and the machine's name,
separated by colons:

```sh
systemctl start cherry-bootstrap@debian:trixie:mybox    # debootstrap
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
- **Tools:** the bash tool runs `bash`, the grep tool the image's ripgrep, and
  git is on the image for its snapshots and worktrees.
- **Providers:** none are configured; nothing on the machine holds a key.
  Connect one at runtime, in OpenChamber. The credential lands under
  `/var/lib/cherry`: on the machine's encrypted disk if it has one, and
  otherwise in RAM, gone at the next boot.

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
  OpenChamber's server in the foreground on port 3000, with the browser UI's
  password from the unit's credential, if any.
- **Left out:** local speech recognition and synthesis (sherpa-onnx, 32 MiB,
  plus models downloaded at run time), and tunnels (no cloudflared or ngrok on
  the image). Update checks are off and `openchamber update` says so, as the
  image is read-only; the device-pairing relay reaches the network.
- **Size:** Bun is 85 MiB, about 42 MB in the image; the npm tree 178 MiB,
  about 42 MB. The idle server uses about 115 MB of RAM.

## Cherry service

`cherry.service`, from the `cherry` package, runs OpenChamber's server as the
`cherry` user, with `/var/lib/cherry` as its home. Like sshd, it starts only
on a machine with a TPM.

- **User and state:** everything runs as `cherry`: OpenChamber, the OpenCode
  it starts, and the agent's tools and terminals. Their data is under
  `/var/lib/cherry` (OpenChamber's in `.config/openchamber`), and the agent
  works in `/var/lib/cherry/work` unless a project picks another directory.
  All of it persists if `/var` is on the machine's disk, and is new at every
  boot otherwise (see [Disks](#disks)). `/var/lib/cherry`
  is the only place the service can write: it sees the rest of the system
  read-only, with a private `/tmp`, and runs without capabilities.
- **Ports:** loopback only, both: OpenChamber on 127.0.0.1:3000, and the
  OpenCode it runs on 127.0.0.1 and a free port. Neither can listen on a Unix
  socket. Reach the UI through SSH port forwarding (below), once the firewall
  lets SSH in, or from another device through the relay of a pairing link.
  `/health` reports OpenChamber's version, `/api/opencode/compatibility` the
  OpenCode it runs.
- **Password:** none on the browser UI, unless the machine was given one as
  the systemd credential `openchamber.ui_password`: for `make qemu`,
  `python3 scripts/run_qemu.py --credential openchamber.ui_password=...`.
- **Restart:** always, after a crash as after a clean exit, such as
  `openchamber stop` or `restart` from a shell.
- **Pairing link on the console:** once the server is online,
  `cherry-connect-url.service` shows the link that pairs another OpenChamber
  app with it, and its QR code, on every console, before the login prompts,
  and in the journal: `openchamber connect-url --relay --qr`, run as the
  `cherry` user. The link carries the relay transport, so a device off the
  local network can use it; it is single-use and expires after 10 minutes.
  `systemctl restart cherry-connect-url.service` shows a fresh one.

To use it, let SSH in (see [Booting](#booting)), forward the port over SSH,
then open `http://127.0.0.1:3000/` in a browser:

```sh
ssh -L 3000:127.0.0.1:3000 -p 2222 root@localhost  # the VM of make qemu
```

## Security notes

This is a development image:
- `root` has no password on the console (Buildroot's default).
- The firewall lets nothing in unasked but ping (see [Firewall](#firewall)).
  SSH, once let in, accepts keys only.
- OpenChamber's browser UI has no password unless the machine was given one
  as a credential: any local user can use it, and so run commands as the
  `cherry` user. It is on loopback only, as is the OpenCode behind it.
- The `cherry` user creates, starts, enters and removes containers through
  polkit (see [As the cherry user](#as-the-cherry-user)): root inside any
  container, and the bootstrap tools run as root on the host for it, with
  checked arguments.
- The TPM unseals the key of the encrypted `/var` disk whatever the machine
  boots: it isn't bound to the boot state (PCRs) yet. The disk is unreadable
  away from its machine, but anything that boots the machine (an HTTP boot
  server, a USB stick) can unlock it.
- Releases are signed for Secure Boot, and a machine in setup mode enrolls
  Cherry's keys (see [Secure Boot](#secure-boot)). Local builds are not
  signed. With Secure Boot on, the kernel still loads unsigned modules: its
  lockdown is off.
