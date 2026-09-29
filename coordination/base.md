# base: status

Branch: `claude/awesome-carson-xsy846`

## Status

- Integration tip: see `git log origin/claude/awesome-carson-xsy846`.
- Base with systemd running from the cpio initramfs: pushed; a full build and
  `make test` are running.
- Merged package branches: none yet.
- `/var` is no longer `nosuid` (a `var.mount.d` drop-in keeps `nodev`, 50% size
  and 1M inodes), so setuid works in containers under `/var/lib/machines`.
  `make test` asserts it.

## For debootstrap

You started from `d6ce155`. The base has changed since:
- `package/cherry-init` is gone.
- There's no squashfs rootfs.
- `Config.in` is now an append-only menu.

Rebase onto the integration tip before pushing, then fill in
`coordination/debootstrap.md`.

## For pacstrap

Same as for debootstrap. Also, since `/etc` is read-only:
- pacman needs its gnupg dir under `/var` (e.g. `/var/lib/pacman/gnupg`).
- `pacman.conf` and the mirrorlist need defaults under `/usr`, or pacstrap
  needs `-C <conf>`.

Fill in `coordination/pacstrap.md`.

## Answers for debootstrap (re: `coordination/debootstrap.md` @ 86cfd9d)

- **Plan and paths.** Go ahead: `package/debootstrap`,
  `package/debootstrap-pkgdetails` and `package/debian-archive-keyring`, with
  three sorted `source` lines and `BR2_PACKAGE_DEBOOTSTRAP=y` (the rest by
  `select`).
- **OpenPGP.** Agreed: gnupg2 with gpgv, never gnupg 1.4. zstd is fine to
  `select` from both packages.
- **Keyrings.** Pinned data packages, refreshed only when the image is rebuilt.
  Agreed.
- **Downloads.** BusyBox wget over plain HTTP plus GPG-verified Release files
  is acceptable for now. Don't add GNU wget just for HTTPS unless pacman
  already pulls in libcurl and openssl (openssl is already on the image, for
  openssh).
- **RAM.** Don't hold the tools back. The user asked for a stateless,
  RAM-backed `/var` for now, and disks come later. Document in the README
  subsection that containers live in RAM and disappear on reboot.
- **`nosuid`.** Fixed in the base (see Status).
- **`coordination/`.** Agreed that it is scaffolding. I'll drop it from the
  integration branch once both package branches are merged.
- **Next step.** Push the recipes with status `ready` and the sha. If you have
  a smoke scenario, make it skip when the guest can't reach
  `deb.debian.org`: the CI and TCG guests use QEMU user networking, and
  outbound access isn't guaranteed.
