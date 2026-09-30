# debootstrap: status

Branch: `claude/eager-ritchie-5q9d1f`, rebased onto `2aa0d4a`.

## Status

**`ready`: merge the tip of `claude/eager-ritchie-5q9d1f`.** The last code
change is `a1450f9`, and the commit that sets this status adds nothing but
this file.

- **Full build:** `make` of `cherry_x86_64_defconfig` + `BR2_PACKAGE_DEBOOTSTRAP=y`
  builds cleanly. The config is the pre-C++ base (`0658801` + Landlock).
  `check-package` reports 0 warnings.
- **Image test:** I unpacked `rootfs.cpio.zst` and chrooted into it, with a
  `nodev` tmpfs on `/var`. Then I ran:
  `debootstrap --variant=minbase --include=systemd,systemd-sysv,dbus trixie /var/lib/machines/debian`
  - There's no `--arch`, no mirror and no `--keyring`.
  - The tools were GNU wget, gnupg2's gpgv, the ar extractor and
    `pkgdetails`. There is no Perl or dpkg on the image.
  - Result: Release signature valid, installed in 22 s, a 237 MB tree, no
    mount left behind.
- **HTTPS mirror:**
  - Against the image's own CA store, the run fails, because this sandbox
    intercepts TLS: certificate checking is working.
  - With `--ca-certificate=<sandbox CA>`, it succeeds.
- **`s4_debootstrap` in QEMU under TCG passes, in 349 s.** I ran it with
  `tests/smoke.py`'s harness against my image, scenario 4 only, because that
  image predates `c7f7b54`, so its sshd fails.
  1. The mirror probe succeeds.
  2. debootstrap runs inside Cherry, taking 314 s under TCG.
  3. `machinectl start debian` boots the container to `multi-user.target`
     in about 6 s.
  4. `systemctl -M debian is-system-running` reports `running`, and
     `systemd-run -M debian ... cat /etc/debian_version` prints `13.7`.
  5. The scenario terminates the container and removes the tree.
- **README path checked:** `machinectl shell debian /usr/bin/cat /etc/debian_version`
  printed `13.7` with rc 0, outside the smoke test.
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
- **Smoke harness, three findings from the TCG runs.** They're your call:
  1. `systemctl -M <machine> is-system-running --wait`, run right after
     `machinectl start`, never returned. The container reached
     `multi-user.target` in 6 s, and the same query without `--wait`
     answers at once. `s4_debootstrap` now waits for
     `Reached target multi-user.target` in `journalctl -u systemd-nspawn@debian`.
     This may bite pacstrap and rpmstrap too.
  2. `machinectl shell` works in the guest, but the harness never saw the
     end marker of that command. machinectl wraps the session in OSC 3008
     sequences, so the `Console` parser probably mishandles them. The
     scenario uses `systemd-run -M` instead.
  3. The image has no `timeout(1)`, because BusyBox `TIMEOUT` is off. GNU
     coreutils from pacstrap will bring it.
- **Guest DNS right after boot.** On QEMU user networking, `resolvectl query`
  first fails with `DNSSEC validation failed: failed-auxiliary`, while
  resolved falls back from DNS-over-TLS. It works a few seconds later. The
  scenario's probe now retries for about a minute; rpmstrap copied that.
  Consider `DNSSEC=no` or `DNSOverTLS=no` in the base's resolved config if
  users hit it.
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
