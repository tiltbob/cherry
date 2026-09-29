# Coordination: debootstrap packaging

- Session: "Debootstrap packaging on buildroot"
- Branch: `claude/eager-ritchie-5q9d1f`, based on `claude/awesome-carson-xsy846` (e00bf53)

## Protocol

- Each session keeps one file, `coordination/<topic>.md`, on its own branch and
  never edits another session's file, so notes never conflict.
- This session re-reads `coordination/*.md` on every origin branch whenever it
  sees a push, and answers in this file.
- `coordination/` is scaffolding: drop it before merging into the default branch.

## What this branch will add

The paths are final; the recipes are not written yet.

- `package/debootstrap/`: debootstrap 1.0.145, a POSIX sh script with no
  build step. It also installs `/usr/share/debootstrap/arch` (`amd64` from
  `BR2_ARCH`), because without dpkg debootstrap can't guess the architecture.
- `package/debootstrap-pkgdetails/`: `pkgdetails.c` from Debian's
  base-installer, about 350 lines of C, so no Perl is needed on the target.
- `package/debian-archive-keyring/`: 2025.1, from the `_all.deb`, installed
  to `/usr/share/keyrings/`.
- `Config.in`: three sorted `source` lines, following the append-only list.
- `configs/cherry_x86_64_defconfig`: `BR2_PACKAGE_DEBOOTSTRAP=y`. Everything
  else comes in through `select`.
- Sources come from `snapshot.debian.org`, as in Buildroot's own
  Debian-sourced packages.

## Shared dependencies (proposals)

- OpenPGP: debootstrap selects `BR2_PACKAGE_GNUPG2` and
  `BR2_PACKAGE_GNUPG2_GPGV`. Never select `BR2_PACKAGE_GNUPG` (1.4): gnupg2
  depends on `!BR2_PACKAGE_GNUPG`. pacman/gpgme needs gnupg2 anyway.
- zstd: `BR2_PACKAGE_ZSTD`, needed for Ubuntu's zstd-compressed .debs
  (`zstdcat`) and for pacman's `.pkg.tar.zst`. Both packages may select it.
- Keyrings: one pinned data package per distro, with no key fetching at
  runtime. Keys refresh only when the image is rebuilt.
- Downloads: BusyBox wget, HTTP only (Buildroot's busybox.config has no TLS).
  This is safe because Release files are GPG-verified. If pacman brings
  libcurl+openssl, GNU wget becomes a cheap add for HTTPS mirrors.

## Verified so far

Tested in a chroot holding only static BusyBox (Buildroot's busybox.config),
gpgv, pkgdetails and debootstrap:

- `debootstrap --arch=amd64 --variant=minbase trixie` succeeds: 78 packages,
  with the Release signature checked by gpgv and the BusyBox `ar` extractor.
- Adding `--include=systemd,systemd-sysv,dbus` also succeeds, which gives a
  tree `machinectl start` can boot.
- It also succeeds onto a `nosuid,nodev` tmpfs like Cherry's `/var`, by
  bind-mounting `/dev` nodes. The tree is 211 MB.
- BusyBox `umount` rejects `--lazy`, which leaves `/proc` mounted in the
  target. Cherry is fine because systemd selects util-linux mount/umount, and
  Buildroot installs BusyBox noclobber.
- With no keyring, debootstrap switches to an HTTPS mirror, which BusyBox
  wget can't fetch. That is why the keyring package is required.
- Not yet tested: Ubuntu with zstd, and running the Buildroot-built packages
  (rather than host-built ones).

## For the pacstrap session

- systemd does not select `BR2_PACKAGE_UTIL_LINUX_UNSHARE` or
  `_MOUNTPOINT`, and BusyBox `UNSHARE` is off. arch-install-scripts need
  `unshare`. `findmnt` is in `UTIL_LINUX_BINARIES`.
- arch-install-scripts are bash, so they need `BR2_PACKAGE_BASH`.

## For the Cherry (main) session

- Everything is RAM-resident now: the cpio root and the tmpfs `/var`. A
  minbase Debian container adds about 211 MB of RAM and is lost on reboot.
  Say so if you want the tools held back until disk-backed `/var` exists.
- `nosuid` on `/var` breaks setuid inside nspawn containers (su, sudo). This
  is worth deciding when `/var` moves to disk.
- Image size: debootstrap, pkgdetails and the keyring come to under 1 MB.
  gnupg2 plus its libraries add a few MB, shared with pacstrap.
