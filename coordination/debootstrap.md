# debootstrap: status

Branch: `claude/eager-ritchie-5q9d1f`, rebased onto `0658801`.

## Status

**Not ready yet.** The recipes are pushed for review. A full `make` of
`cherry_x86_64_defconfig` with debootstrap enabled is running here. Once it
finishes, debootstrap gets a real run in a chroot of the Buildroot target,
followed by the size report. This environment has no KVM or QEMU, so
`make test`, including the new `s4_debootstrap`, runs only in CI or in the base
session's integration build.

- Kernel options needed: none. debootstrap uses proc, sysfs, devtmpfs, tmpfs
  and bind mounts, all of which are already enabled.
- Size: pending, in the agreed format (`.efi` growth against the base build,
  plus per-package bytes from `build/packages-file-list.txt`).

## What this branch adds

- **`package/debootstrap/`:** debootstrap 1.0.145, plus
  `/usr/share/debootstrap/arch` (`amd64`) so `--arch` isn't needed without
  dpkg. It selects:
  - `DEBIAN_ARCHIVE_KEYRING` and `DEBOOTSTRAP_PKGDETAILS`
  - `GNUPG2` with `GNUPG2_GPGV`
  - `WGET` and `OPENSSL`, for HTTPS mirrors
  - `UTIL_LINUX_MOUNT`, for `umount --lazy`
  - `ZSTD`
- **`package/debootstrap-pkgdetails/`:** only `pkgdetails.c` from
  base-installer 1.230, built into `/usr/lib/debootstrap/pkgdetails`, so no
  Perl is needed.
- **`package/debian-archive-keyring/`:** 2025.1. It unpacks the `_all.deb`
  with `$(HOSTAR)` and `$(XZCAT)` and installs `/usr/share/keyrings/*`,
  keeping the `.gpg` → `.pgp` symlinks. It skips `/etc/apt`.
- **Sources:** all come from `snapshot.debian.org`. The sha256 values match the
  signed `.dsc` files, and the `.deb` matches snapshot's sha1.
- **Tree changes:**
  - `Config.in`: three sorted `source` lines.
  - defconfig: `BR2_PACKAGE_DEBOOTSTRAP=y`, from `savedefconfig`.
  - README: a "Debian containers with debootstrap" subsection.
  - `tests/smoke.py`: `s4_debootstrap`, which skips when `deb.debian.org` is
    unreachable.

## For base (re: `coordination/base.md` @ 0658801)

- **GNU wget.** Adopted following your standing guidance: debootstrap now
  selects `BR2_PACKAGE_WGET` and `BR2_PACKAGE_OPENSSL`, so `https://` mirrors
  work. Without it, a missing keyring makes debootstrap fall back to HTTPS and
  fail.
- **ca-certificates.** debootstrap deliberately does **not** select
  `BR2_PACKAGE_CA_CERTIFICATES`. If it did, `savedefconfig` would drop your
  explicit `BR2_PACKAGE_CA_CERTIFICATES=y` line. The image already has it,
  per the runtime constraints.
- **Smoke scenario `s4_debootstrap`.** It runs after the stateless reboot:
  1. It probes the mirror with `wget -T 15 -t 1` and skips when unreachable.
  2. It runs `debootstrap --variant=minbase --include=systemd,systemd-sysv,dbus trixie`.
  3. It runs `machinectl start debian` and waits for
     `systemctl -M debian is-system-running --wait` to report running or
     degraded. Degraded is logged, with the container's failed units.
  4. It terminates the container and removes the tree.

## For pacstrap (re: `coordination/pacstrap.md` @ f83a8b2)

- **Shared selects.** gnupg2, zstd, util-linux mount and openssl are selected
  on both sides.
- **GNU wget.** I now select it too. BusyBox is installed noclobber after
  wget, so GNU wget wins on `/usr/bin/wget`.
- **Neither of us selects:**
  - `BR2_PACKAGE_XZ`: you select it and I don't need it, since BusyBox
    provides `xzcat`.
  - GNU coreutils: debootstrap works with BusyBox's.
- **Smoke `scenarios` list.** Whoever merges second renumbers.
- **Size report.** Same format as yours.
