# debootstrap: status

Branch: `claude/eager-ritchie-5q9d1f`, rebased onto `aa177e0`.

## Status

**Validated locally.** One step is still running: `s4_debootstrap` in QEMU
under TCG. I'll set the status to `ready` with the sha once it passes.

- **Full build:** `make` of `cherry_x86_64_defconfig` + `BR2_PACKAGE_DEBOOTSTRAP=y`
  builds cleanly. The config is the pre-C++ base (`0658801` + Landlock).
  `check-package` reports 0 warnings.
- **Image test, default settings:** I unpacked `rootfs.cpio.zst` and chrooted
  into it, with a `nodev` tmpfs on `/var`. Then I ran:
  `debootstrap --variant=minbase --include=systemd,systemd-sysv,dbus trixie /var/lib/machines/debian`
  - There's no `--arch`, no mirror and no `--keyring`: `arch` comes from
    `/usr/share/debootstrap/arch`, and `.pgp` from `/usr/share/keyrings`.
  - The tools were GNU wget, gnupg2's gpgv, the ar extractor and
    `pkgdetails`. There is no Perl or dpkg on the image.
  - Result: Release signature valid, "Base system installed successfully"
    after 22 s, a 237 MB tree, and no mount left behind.
- **HTTPS mirror:**
  - Against the image's own CA store, the run fails: this sandbox intercepts
    TLS with its own CA, so certificate checking is working.
  - With `--ca-certificate=<sandbox CA>`, it succeeds, which exercises
    GNU wget's TLS path end to end.
- **Kernel options needed:** none.

## Size report (agreed format; base numbers from `coordination/base.md`, pre-C++)

| | base | + debootstrap | delta |
|---|---:|---:|---:|
| `cherry-x86_64.efi` | 32,388,096 | 35,298,304 | +2,910,208 (+2.8 MiB) |
| `rootfs.cpio` (uncompressed root in RAM) | 68,909,568 | 78,507,520 | +9,597,952 (+9.2 MiB) |
| `bzImage` | 15,602,688 | 15,602,688 | 0 |

debootstrap adds these packages; none are removed. The sizes are the bytes
each installs in the final target, taken from `build/packages-file-list.txt`:

| package | bytes |
|---|---:|
| gnupg2 (full suite; debootstrap uses only `gpgv`, 552 KB) | 5,751,252 |
| libgcrypt | 1,831,486 |
| zstd | 920,566 |
| wget | 573,481 |
| libksba | 283,379 |
| libgpg-error | 233,845 |
| debootstrap | 211,694 |
| debian-archive-keyring | 184,874 |
| libassuan | 84,092 |
| libnpth | 26,504 |
| debootstrap-pkgdetails | 18,360 |
| **total** | **10,119,533** |

After pacstrap merges, gnupg2 and its libraries (about 8.2 MB) are shared:
pacman needs full gnupg2. zstd, too, is shared with pacstrap and rpmstrap.

## What this branch adds

- **`package/debootstrap/`:** debootstrap 1.0.145, plus
  `/usr/share/debootstrap/arch` (`amd64`). It selects:
  - `DEBIAN_ARCHIVE_KEYRING` and `DEBOOTSTRAP_PKGDETAILS`
  - `GNUPG2` with `GNUPG2_GPGV`
  - `WGET` and `OPENSSL`
  - `UTIL_LINUX_MOUNT`, for `umount --lazy`
  - `ZSTD`
- **`package/debootstrap-pkgdetails/`:** only `pkgdetails.c` from
  base-installer 1.230.
- **`package/debian-archive-keyring/`:** 2025.1. It unpacks the `_all.deb`
  with `$(HOSTAR)` and `$(XZCAT)` and installs `/usr/share/keyrings/*`,
  keeping the `.gpg` → `.pgp` symlinks.
- **Sources:** all come from `snapshot.debian.org`. The sha256 values match the
  signed `.dsc` files, and the `.deb` matches snapshot's sha1.
- **Tree changes:**
  - `Config.in`: three sorted `source` lines.
  - defconfig: `BR2_PACKAGE_DEBOOTSTRAP=y`, from `savedefconfig`.
  - README: a "Debian containers with debootstrap" subsection.
  - `tests/smoke.py`: `s4_debootstrap`.

## For base (re: `coordination/base.md` @ aa177e0)

- **GNU wget** is adopted, per your standing guidance.
- **ca-certificates.** debootstrap deliberately doesn't select
  `CA_CERTIFICATES`, so `savedefconfig` keeps your explicit line.
- **Users:** debootstrap needs no host users.
- **CI cost.** Every push starts the full `build` workflow, including
  coordination-only pushes, and 14 runs queued within an hour. Two changes
  would help:
  - `paths-ignore: ['coordination/**']` on `push`
  - `concurrency: {group: build-${{ github.ref }}, cancel-in-progress: true}`,
    so a force-pushed branch cancels its stale run
  The workflow is yours, so I haven't touched it.
- **Download fallback.** In my environment, GitHub archive URLs (systemd,
  gnu-efi, ninja) return 403, and Buildroot falls back to
  `sources.buildroot.net`, which works here.

## For pacstrap (re: `coordination/pacstrap.md` @ deb55b4)

- **gnutls.** Selecting it is fine for debootstrap. Buildroot's wget then
  builds `--with-ssl=gnutls`. Buildroot's gnutls sets
  `--with-default-trust-store-file=/etc/ssl/certs/ca-certificates.crt` when
  ca-certificates is enabled, so HTTPS mirrors keep verifying.
- **Merge order.** Whoever merges second renames its `s4_*` scenario.

## For rpmstrap (re: `coordination/rpmstrap.md` @ b61c662)

- **No overlap** beyond openssl and zstd. debootstrap doesn't use the C++
  toolchain.
