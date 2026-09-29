# pacstrap: status

Branch: `claude/jolly-ritchie-cw9f5o`, on `f0f7ef4`. I'll rebase onto `aa177e0`
once the build running here finishes.

## Status

**Not ready yet.**

- The baseline `f0f7ef4` build is done: `cherry-x86_64.efi` is 32,188,416
  bytes and `rootfs.cpio` is 68,329,472 bytes.
- The recipes are now wired in: three `source` lines in `Config.in`, and
  `BR2_PACKAGE_PACMAN=y` plus `BR2_PACKAGE_ARCH_INSTALL_SCRIPTS=y` in the
  defconfig via `savedefconfig`.
- An incremental build on top of the baseline is running, to measure the size
  delta. My packages are all C, so base's C++ toolchain doesn't affect it.
- QEMU and OVMF are installed in this environment. `make test` and a proxied
  `s4_pacstrap` run follow, reporting `pacman-init` timing under TCG. Then
  comes a clean rebuild on the integration tip.

Changes since `f83a8b2`, following base's answers:
- **`pacman-init.service`** uses `DefaultDependencies=no`, ordered after
  `sysinit.target` and `time-sync.target`. Without that, a target orders
  itself after everything it `Wants=`, so `multi-user.target` would have
  waited for it. It's offline and idempotent. The README will tell pacstrap
  users to run `systemctl start pacman-init` first.
- **pacman now selects `BR2_PACKAGE_GNUTLS`.** When a package is signed by an
  unknown key, pacman fetches that key through gpgme, via WKD and then a
  keyserver. Both lookups go through gnupg2's dirmngr, which Buildroot builds
  without TLS unless gnutls is enabled. With gnutls, a packager key newer than
  the image's keyring is fetched and validated through the lsigned master
  keys, as on Arch. Without it, pacstrap fails until the image is rebuilt.
- **Landlock.** Confirmed off in the `f0f7ef4` kernel (`# CONFIG_SECURITY_LANDLOCK
  is not set`, while `CONFIG_LSM` already lists landlock). Thanks for adding
  it. `s4_pacstrap` will run the container's own `pacman -Sy` under
  systemd-nspawn, to check that nspawn's seccomp filter lets the Landlock
  syscalls through.
- **Host users.** None needed. The Cherry `pacman.conf` sets no
  `DownloadUser`, and pacstrap comments it out and passes `--disable-sandbox`
  anyway.

## What this branch adds (paths are final, contents are WIP)

- **`package/pacman/`:** pacman 7.1.0, a meson build.
  - Build options: `-Ddoc=disabled -Dcurl=enabled -Dgpgme=enabled -Dcrypto=openssl`.
  - Scriptlet shell and ldconfig use Arch's paths (`/usr/bin/bash`,
    `/usr/bin/ldconfig`), because scriptlets run chrooted in the Arch tree.
  - makepkg, repo-add, testpkg and the makepkg templates are removed from
    the target. `pacman-key` and the libmakepkg `util/` helpers it sources are
    kept.
  - Also installs:
    - `/etc/pacman.conf`: core and extra repos.
    - `/etc/pacman.d/mirrorlist`.
    - `pacman-init.service`, which runs `pacman-key --init` and
      `--populate archlinux`.
    - the `/etc/pacman.d/gnupg` symlink (see below).
- **`package/arch-install-scripts/`:** v31, built with `m4` only, installing
  `pacstrap` and `arch-chroot`. Upstream's `install` target always builds the
  man pages, which needs asciidoc; Buildroot has no asciidoc package.
- **`package/archlinux-keyring/`:** 20260909. The GitLab release tarball
  already contains `archlinux.gpg`, `-trusted` and `-revoked`, so no
  `sq`/keyringctl is needed. It installs to `/usr/share/pacman/keyrings`.
- Config and defconfig changes will come after the size measurement: three
  sorted `source` lines, plus `BR2_PACKAGE_PACMAN=y` and
  `BR2_PACKAGE_ARCH_INSTALL_SCRIPTS=y`.

## Answers for base (re: `coordination/base.md` @ f0f7ef4)

- **Keyring under `/var`.** Done without patching upstream. The image ships
  `/etc/pacman.d/gnupg` as a symlink to `/var/lib/pacman/gnupg`, and
  `pacman-init.service` (`StateDirectory=pacman/gnupg`) fills it at every boot.
  - `pacman-key` explicitly supports a symlink there.
  - Both of its steps are idempotent, so the same unit keeps working once
    `/var` is on disk.
  - A symlink is needed: pacman always verifies packages against the HOST
    gpgdir, never one relative to `--root`, and pacstrap hard-codes
    `/etc/pacman.d/gnupg`.
  - Baking a keyring into the image is not an option. Its local signing
    key would be public, and pacman auto-imports unknown keys from WKD, so a
    leaked signing key could validate an attacker's key.
- **`pacman.conf` and mirrorlist.** They ship as read-only defaults in `/etc`.
  pacstrap hard-codes `pacman_config=/etc/pacman.conf` and copies
  `/etc/pacman.d/mirrorlist` into the new root, and pacman's compiled
  `CONFFILE` is `/etc/pacman.conf`. To change repos or mirrors, users pass
  `pacstrap -C <conf>` and `-M`.
  - Question: is that OK, or do you want them under `/usr` with a patched
    pacstrap default? I'd rather not carry a patch.
- **Kernel options.** pacstrap itself needs nothing new: `PID_NS`, `USER_NS`,
  `DEVTMPFS`, proc, sysfs, devpts and tmpfs are all present.
  - There is one request, for containers rather than the host: Arch's own
    `pacman.conf` inside the container sets `DownloadUser = alpm`.
  - pacman 7 then sandboxes downloads with Landlock, and **aborts the
    download** ("switching to sandbox user 'alpm' failed") when the kernel has
    no Landlock.
  - So `pacman -Syu` inside an Arch container needs `CONFIG_SECURITY_LANDLOCK=y`,
    with `landlock` in `CONFIG_LSM`. The alternative is
    `DisableSandboxFilesystem` in every container's `pacman.conf`.
  - I'll confirm this against the built kernel's `.config` and report back.
- **RAM.** Arch's bootstrap tarball unpacks to 553 MB, so expect roughly that
  for `pacstrap -K <dir> base` on the tmpfs `/var`. The exact figure will come
  from the chroot run. pacstrap also leaves the downloaded packages in
  `<root>/var/cache/pacman/pkg`, which also counts against RAM. The README
  subsection will say to clear them.
- **Smoke test.** A planned `sN_pacstrap` scenario will skip when the guest
  can't reach the mirror. I'll add it after rebasing on whichever package
  branch merges first, to avoid a conflict on the `scenarios` list.

## For debootstrap (re: `coordination/debootstrap.md` @ 86cfd9d)

- **Shared dependencies, agreed.** gnupg2 (never 1.4) and zstd. pacman needs
  full `gpg` and `gpg-agent` through gpgme, not just gpgv, so once both
  branches land gnupg2's cost is shared and gpgv is an extra few hundred KB.
- **libcurl and openssl.** pacman pulls in libcurl, and openssl is already
  there. By base's rule, GNU wget for HTTPS mirrors is now a cheap add if you
  want it.
- **Also selected by pacstrap, in case you need them:**
  - GNU coreutils, for `cp --no-preserve=ownership`, which BusyBox cp lacks.
    It is selectable because systemd selects `BUSYBOX_SHOW_OTHERS`.
  - util-linux `unshare`, which you already flagged.
- **libarchive xattrs.** Buildroot's libarchive drops xattrs unless
  `BR2_PACKAGE_ATTR=y`. Arch ships `newuidmap`/`newgidmap` with
  `security.capability` xattrs, so pacman selects attr. debootstrap probably
  doesn't care, since Debian sets capabilities in postinst.
- **Size measurement.** Let's report it the same way so the user can compare:
  - `images/cherry-x86_64.efi` growth against a clean `f0f7ef4` build.
  - Per-package bytes from `build/packages-file-list.txt`.
  - I'll post mine here.
- **Keyring paths.** Distro keyrings live in their tool's default location:
  `/usr/share/keyrings` for you, `/usr/share/pacman/keyrings` for pacman. They
  don't collide.
