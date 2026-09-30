# rpmstrap: status

Branch: `claude/wizardly-meitner-uy0avn`, rebased onto `3865d5a` (after
rpmstrap's merge as `1c6a42a`).

## Status

**Ready: one fix to merge, `a053248` (rpmstrap installs dbus by default).**
Without it, `s5_rpmstrap` would fail once it runs for real. See "For base
(re: `coordination/base.md` @ 3865d5a)" below.

rpmstrap was tested for real in a chroot of the finished Buildroot target,
after `target-finalize`:

| Distribution | Time | Packages | Target's own `rpm -Va` | Target's own dnf |
|---|---|---|---|---|
| Fedora 44 | 25 s | 125, rpm 6.0 | clean | dnf5 sees them |
| CentOS Stream 9 | 15 s | 150, rpm 4.16, `/var/lib/rpm` | clean | dnf 4.14 sees them |
| AlmaLinux 9 | 19 s | 150 | clean | (not run) |
| Rocky Linux 10 | 20 s | 138, rpm 4.19 | clean | dnf 4.20 sees them |

- **What was checked:**
  - rpmstrap's own check that the target's rpm expects the database where it
    was written.
  - Keys imported from `distribution-gpg-keys`, and every package's
    signature verified.
  - sysusers.d users created before files are unpacked. For example,
    `/var/log/journal` is `root:systemd-journal` 2755.
  - "clean" means no owner, group or mode mismatches. Only `/run/*`
    entries are missing, because `/run` is a tmpfs.
- **Cherry's own `systemd-nspawn`** runs commands in the Fedora tree
  (`--register=no`; this sandbox has no systemd PID 1 or machined).
- **Every profile key imports under rpm-sequoia's default policy.** The
  CentOS profile uses the SHA-256 re-signed copy of the CentOS key; the
  original's SHA-1 binding is rejected. So no crypto-policy override is
  shipped.
- **Not run here:** `make test`. This environment has no QEMU or KVM.
  `s5_rpmstrap` follows the pattern of the merged `s4_debootstrap`:
  - a retried probe, skipping if `mirrors.fedoraproject.org:443` stays
    unreachable
  - `rpmstrap fedora 44`, then `machinectl start`
  - a wait for "Reached target multi-user.target" in the host journal
  - `systemd-run -M fedora -P rpm -q fedora-release systemd dnf5`
  - terminate the container and remove it
- **Combined build (debootstrap + rpmstrap)** from the integration tip's
  defconfig plus this branch builds cleanly, with `cherry-x86_64.efi` at
  47,783,936 bytes. `rpmstrap fedora 44` on that target gives the same
  result: 125 packages, and `rpm -Va` clean.

## Size report (against the C++ base `aa177e0`, as you asked)

Measured on a full build of the C++ base plus this branch, without
debootstrap.

| Artifact | `aa177e0` | + rpmstrap | Growth |
|---|---|---|---|
| `cherry-x86_64.efi` | 32,923,648 | 44,920,320 | +11,996,672 |
| `rootfs.cpio` (RAM) | 70,731,264 | 113,222,144 | +42,490,880 |
| `rootfs.cpio.zst` | 17,193,387 | 29,190,104 | +11,996,717 |

Per package, in bytes on the target. `packages-file-list.txt` was scrambled
by interrupted rebuilds here (a disk-full restart and reinstalls), so these
are measured by path instead: every file that isn't in the base's file list,
attributed by install path. The total, 41.8 MB without libstdc++, matches
the cpio growth.

| Package | Bytes |
|---|---|
| file (libmagic, plus its 10 MB `magic.mgc`) | 10,564,960 |
| dnf5 | 7,378,750 |
| libglib2 (for librepo) | 4,695,517 |
| rpm-sequoia | 3,072,352 |
| sqlite | 2,835,208 |
| rpm6 | 2,053,522 |
| libxml2 | 1,391,368 |
| libsolv-rpm | 1,215,712 |
| distribution-gpg-keys | 1,103,026 |
| bash | 997,584 |
| ncurses, readline | 966,729 |
| zstd | 920,554 |
| libarchive | 849,768 |
| libcurl | 703,685 |
| xz, bzip2, zchunk | 675,632 |
| pcre2 | 670,048 |
| lua | 501,584 |
| util-linux (libsmartcols, unshare) | 401,664 |
| fmt, json-c, popt, libffi | 311,824 |
| shadow-useradd | 276,360 |
| librepo | 191,152 |
| rpmstrap | 8,740 |
| other | 22,555 |

- **libmagic's database** (`/usr/share/misc/magic.mgc`, 10 MB) is the
  biggest single item. rpm links libmagic unconditionally for rpmbuild, but
  installing packages never reads the database. I kept it, per the standing
  guidance. If RAM ever matters, rpm6 can delete it in a hook, which would
  break only `file` and rpmbuild.
- **Containers, in `/var` (RAM):**
  - Fedora 44 tree: 194 MB, with a 376 MB peak while installing (metadata
    and packages, deleted afterwards).
  - EL9 and EL10 trees: about 265 MB.

## What this branch adds

```sh
rpmstrap fedora 44 /var/lib/machines/fedora
machinectl start fedora
```

The tool is upstream **dnf5** with `--installroot`, wrapped by the small
`rpmstrap` script.

| Package | Version | What it is |
|---|---|---|
| `package/rpm-sequoia` | 1.10.3 | rpm's OpenPGP backend, a Rust cdylib with the `crypto-openssl` feature. |
| `package/rpm6` | 6.1.0 | rpm, cmake/C++20. Its home is `/usr/libexec/rpm` (see "For base"). |
| `package/libsolv-rpm` | 0.7.40 | libsolv built as Fedora builds it: rpm, rpm-md, comps and every compression. |
| `package/librepo` | 1.21.1 | Metadata downloader. Verifies OpenPGP through rpm, so there's no gnupg. |
| `package/toml11` | 4.4.0 | Header-only, staging only. |
| `package/dnf5` | 5.4.6.0 | `dnf5` plus `/usr/bin/dnf`. No daemon, CLI plugins, modularity, systemd/sdbus or bindings. |
| `package/distribution-gpg-keys` | 1.123 | Every RPM distribution's signing keys, as used by mock, without the 144 MB of copr keys. |
| `package/shadow-useradd` | 4.18.0 | Only `useradd`, `groupadd` and `usermod`, from Buildroot's shadow source and hash. |
| `package/rpmstrap` | in-tree | The wrapper, plus profiles in `/usr/share/rpmstrap/<distro>/`: `fedora` (41+), and `centos-stream`, `almalinux` and `rocky` (9 and 10). EL8 is refused: its repositories need modularity. |

**What the wrapper does:**
1. Runs dnf5 with `--use-host-config --setopt=reposdir=<profile dir>`, so
   only that distro's repositories load and nothing is read from `/etc`.
2. Sets `%_dbpath` to what the target's rpm expects, through a private
   `$XDG_CONFIG_HOME/rpm/macros`. Afterwards, it asks the target's rpm and
   fails loudly on a mismatch.
3. Mounts `/proc`, a read-only `/sys`, a minimal `/dev`, `/run` and `/tmp`
   inside a private mount namespace.
4. Keeps the metadata cache in a temporary directory and deletes it.
5. Uses HTTPS-only mirrors:
   - This works behind proxies that only tunnel HTTPS.
   - For Alma and Rocky, TLS is what protects the metadata, since their
     mirrorlists carry no checksums.

## For base (re: `coordination/base.md` @ c13bdb5)

- **Two Buildroot bugs, fixed here; both are still present on Buildroot
  master:**
  - **`target-finalize` deletes `/usr/lib/rpm`**, as development files. That
    is rpm's home: rpmrc, macros and `sysusers.sh`. On the finished image,
    every rpm and dnf5 command failed with "failed to read rpm config
    files". Buildroot's own rpm package has the same problem. rpm6 now uses
    `-DRPM_CONFIGDIR=/usr/libexec/rpm`.
  - **Buildroot's lua shared-library patch links `liblua.so` without
    `-lm`.** rpm, which loads liblua before libm, then aborts at startup
    (`Relink liblua.so.5.4.8 with libm.so.6 for IFUNC symbol sin`). The fix
    is `patches/lua/5.4.8/0001-src-Makefile-link-liblua.so-with-libm.patch`,
    in the global patch dir. Please own or move it as you see fit. It only
    applies to lua 5.4.8.
- **Coordination table:** please add `shadow-useradd` to my package list,
  plus `patches/lua/5.4.8/`.
- **Kernel options:** none needed. rpmstrap uses mount namespaces, proc,
  sysfs, devpts and tmpfs, all present. `CONFIG_NAMESPACES`, `UNIX98_PTYS`
  and `TMPFS` were checked in the built `.config`.
- **Users:** none added on the host. rpm's upstream `sysusers.sh` creates
  the target's users with `useradd -R <root>`, which only writes the
  target's `/etc`.
- **Download hashes:** the git-method tarballs (`-git4`, `-cargo4`) come
  from Buildroot's reproducible archiver. If your environment computes a
  different hash for any of them, tell me here.
- **Combined build:**
  - The integration tip's defconfig with this branch (debootstrap plus
    rpmstrap) round-trips through `savedefconfig` unchanged.
  - It builds, with an `.efi` of 47,783,936 bytes.
  - Both tools are installed side by side, with no file conflicts.

## For base (re: `coordination/base.md` @ 3865d5a)

- **`timeout(1)` in the probe was my mistake.** I tested `/dev/tcp` in the
  chroot without it. Thanks for the `wget` probe.
- **The new probe, checked on the built image in a chroot:**
  - With the image's own CA bundle, it returns 0. wget verifies Fedora's
    real certificate (DigiCert), because this sandbox's proxy tunnels
    `mirrors.fedoraproject.org` without intercepting it.
  - With no trusted CAs, it returns 1 ("cannot verify ... certificate"), so
    s5 skips.
  - Cherry's wget links OpenSSL.
- **Found by booting the container, and fixed in `a053248`: Fedora trees had
  no D-Bus.**
  - Fedora's systemd only *recommends* dbus, and rpmstrap turns weak
    dependencies off. EL's systemd requires it, so only Fedora was hit.
  - Booted with Cherry's `systemd-nspawn -b` (no machined here, so
    `--register=no`), it reached multi-user.target, but `systemd-logind`
    failed six times ("Failed to connect to system bus").
  - Without a bus in the container, s5's `systemctl -M fedora` and
    `systemd-run -M fedora` would fail in CI, and so would the README's
    `machinectl shell`.
  - All four profiles now list `dbus`. Fedora gains 4 packages (129), and
    the tree stays about 194 MB.
- **Verified after the fix**, in the same way for Fedora 44 and CentOS
  Stream 9:
  - A test-only unit, run after boot, records
    `systemctl is-system-running --wait`. It reads `running`, with no failed
    units.
  - `org.freedesktop.login1` is on the bus.
  - `systemd-run --wait -P rpm -q ...` inside the container prints the
    release, systemd and dnf packages.
  - The container then powers off cleanly.
- **Fedora 44's console line** is `Reached target multi-user.target -
  Multi-User System.`, which your regex matches. CentOS Stream 9's
  systemd 252 prints the description only, which it matches too.
- **Still not verified here:** the `-M` machine transport through machined,
  since this sandbox has no systemd PID 1. That, and the rest of s5, needs
  your CI run with `a053248`.

