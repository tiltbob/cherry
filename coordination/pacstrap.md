# pacstrap: status

Branch: `claude/jolly-ritchie-cw9f5o`, rebased onto `aa177e0`.

## Status

**Not ready yet. pacstrap works end to end from the Buildroot-built tools.**
Still running here:
- a clean build on `aa177e0` (C++ toolchain, Landlock kernel)
- `make test` plus a proxied `s4_pacstrap` in QEMU under TCG. QEMU and OVMF
  are installed in this environment.
- a WKD key-fetch test

I'll set `ready` with the sha after those pass.

- **Chroot run.** I ran pacstrap from a chroot of the Buildroot target
  (`f0f7ef4` + this branch), with a `nodev` tmpfs `/var`, through this
  sandbox's TLS-intercepting proxy.
  - `pacman-key --init` took 1 s and `--populate archlinux` took 4 s,
    leaving 183 keys and 38 revoked keys disabled.
  - `pacstrap -K -c /var/lib/machines/arch base` installed 137 packages in
    26 s. The tree is 594 MB, plus a 123 MB host package cache.
  - `newuidmap` keeps `cap_setuid=ep`, so libarchive's xattr support (via
    attr) works.
  - The container gets its own 183-key keyring and its own master key
    (`-K`).
  - Warnings, both harmless: pacman's "directory permissions differ on
    `<root>/run/`", and systemd's "Current root is not booted" from hooks
    running in a chroot.
- **Found while building.** GnuPG 2.5 **does not build dirmngr at all**
  without a TLS library (config.log: "Neither NTBTLS nor GNUTLS available -
  not building dirmngr"). The gnutls select is therefore what lets pacman
  fetch any key over the network.
- **Kernel options needed:** none beyond Landlock, which base already added.

## Size report (agreed format)

My baseline is my own `f0f7ef4` build, and the delta is an incremental build of
this branch on it. My packages are all C, so the C++ toolchain doesn't change
them. I'll add absolute numbers from the clean `aa177e0` build once base posts
its base+C++ numbers.

| | base (`f0f7ef4`) | + pacstrap | delta |
|---|---:|---:|---:|
| `cherry-x86_64.efi` | 32,188,416 | 40,810,496 | +8,622,080 (+8.2 MiB) |
| `rootfs.cpio` (root in RAM) | 68,329,472 | 91,559,424 | +23,229,952 (+22.2 MiB) |

The table below lists bytes of new files in the final target, from
`build/packages-file-list.txt`, grouped by why each package is pulled in:

| why | package | bytes |
|---|---|---:|
| the tools | pacman | 596,555 |
| | arch-install-scripts | 17,481 |
| | archlinux-keyring | 1,813,286 |
| OpenPGP (gpgme runs gpg) | gnupg2 | 5,850,340 |
| | libgcrypt | 1,831,486 |
| | libgpgme | 595,680 |
| | libksba | 283,379 |
| | libgpg-error | 233,845 |
| | libassuan | 84,092 |
| | libnpth | 26,504 |
| TLS for dirmngr (WKD) | libunistring | 2,054,162 |
| | gnutls | 1,955,336 |
| | nettle | 747,518 |
| | gmp | 522,200 |
| | libtasn1 | 88,026 |
| packages (`.pkg.tar.zst`, xattrs) | zstd | 920,598 |
| | libarchive | 821,136 |
| | xz | 277,079 |
| | attr | 41,455 |
| downloads | libcurl | 711,909 |
| pacman-key, pacstrap (bash) | bash | 997,584 |
| | ncurses | 531,954 |
| | readline | 436,150 |
| `cp --no-preserve` | coreutils | 1,514,307 |
| `unshare` | util-linux (delta) | 97,440 |
| | **total** | **23,049,502** |

Once debootstrap is merged, the gnupg2 stack, gnutls (its wget then uses
gnutls), zstd and openssl are shared.

If size ever matters: gnupg2's gpgsm, scdaemon, gpg-wks-*, gpgscm,
gpgme-tool/json and kbxutil could be removed after install. That isn't done,
per the standing guidance.

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
- **`Config.in`:** three sorted `source` lines.
- **Defconfig:** `BR2_PACKAGE_PACMAN=y` and `BR2_PACKAGE_ARCH_INSTALL_SCRIPTS=y`,
  from `savedefconfig`. Everything else comes in through `select`.
- **`tests/smoke.py`:** `s4_pacstrap`, which:
  1. requires `pacman-init` to be active with more than 20 keys. This part
     runs offline.
  2. probes the mirror with `pacman -Sy` into a temporary dbpath, which
     honours `https_proxy`, and skips when the mirror is unreachable.
  3. runs `pacstrap -K -c base` and checks `newuidmap`'s capability.
  4. runs the container's own `pacman -Sy` under `systemd-nspawn`, which
     exercises the Landlock download sandbox and nspawn's seccomp filter.
  5. runs `machinectl start arch` and waits for running or degraded.
  6. cleans up.
- **README:** an "Arch Linux containers with pacstrap" subsection.

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
