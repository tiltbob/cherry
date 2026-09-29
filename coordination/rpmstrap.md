# rpmstrap: status

Branch: `claude/wizardly-meitner-uy0avn`, based on `0658801`.

## Status

**Not ready: plan for review.** Recipes follow on this branch. A baseline build
of `0658801` is running here for the size report. Afterwards, rpmstrap gets a
real run in a chroot of the Buildroot target: Fedora, then an EL9 and an EL10
rebuild. This environment has no QEMU or KVM, so `make test` runs only in CI or
in the base session's integration build.

## Goal

Bootstrap RPM-based distributions (Fedora, CentOS Stream, AlmaLinux, Rocky
Linux) into `/var/lib/machines/<name>`, the way debootstrap and pacstrap do
for Debian and Arch:

```sh
rpmstrap fedora 43 /var/lib/machines/fedora
machinectl start fedora
```

## Plan

The tool is upstream **dnf5** with `--installroot`, plus a small `rpmstrap`
wrapper. The wrapper does the few things plain `dnf5 --installroot` gets wrong
on a foreign host.

| Package | Version | What it is |
|---|---|---|
| `package/rpm-sequoia` | 1.10.3 (crates.io) | rpm's OpenPGP backend, a Rust cdylib. Uses the `crypto-openssl` feature, so it reuses the image's openssl. |
| `package/rpm6` | 6.1.0 | rpm, cmake/C++20. Needed with `rpm-sequoia`; details below. |
| `package/libsolv-rpm` | 0.7.40 | libsolv with the rpm, rpm-md and comps backends, built the way Fedora does (`-DFEDORA=1`). |
| `package/librepo` | 1.21.1 | Repository metadata downloader. It verifies OpenPGP through rpm (`USE_GPGME=OFF`), so there's one OpenPGP stack. |
| `package/toml11` | 4.4.0 | Header-only, staging only. Build dependency of dnf5. |
| `package/dnf5` | 5.4.6.0 | `dnf5` plus its libraries. No daemon, Python, modularity, systemd/sdbus or docs. |
| `package/distribution-gpg-keys` | 1.123 | The signing keys of every major RPM distribution, as used by mock. Installed in `/usr/share/distribution-gpg-keys`, without the 144 MB of copr keys. |
| `package/rpmstrap` | (in-tree) | The wrapper, plus per-distribution repository definitions in `/usr/share/rpmstrap/<distro>/*.repo`. |

What `rpmstrap <distro> <release> <dir> [pkg...]` does:
1. Points dnf5 only at that distro's repo files (`--use-host-config
   --setopt=reposdir=/usr/share/rpmstrap/<distro>`), with gpgkeys from
   `distribution-gpg-keys`. Repo IDs from different distros never collide, and
   nothing is read from `/etc`.
2. Sets the rpmdb path and backend the target expects. Fedora and EL10 use
   `/usr/lib/sysimage/rpm`, EL8/9 use `/var/lib/rpm`. It does this through a
   private `$XDG_CONFIG_HOME/rpm/macros`, a documented rpm lookup path.
   Without it, an EL9 container's own rpm/dnf sees an empty package database.
3. Mounts `/proc`, `/sys` (ro), a minimal `/dev` and `/run` into the root in a
   private mount namespace, as pacstrap and mock do, so scriptlets work. The
   mounts vanish with the namespace.
4. Keeps dnf's metadata cache in a temporary directory and deletes it
   afterwards, so nothing is left on the RAM-backed `/var`.

## Why rpm and libsolv are new recipes rather than Buildroot's

- **Buildroot's `rpm` is 4.18.1**, as it is on Buildroot master.
  - dnf5 5.4 requires rpm >= 4.19.
  - Fedora 42+ packages rely on rpm's native sysusers.d handling (4.19+) to
    create users before files are unpacked.
  - rpm >= 4.19 needs rpm-sequoia for OpenPGP. Without it, rpm 6 builds a
    dummy backend that verifies no signatures at all.
- **Buildroot's `libsolv` is 0.7.35** with no rpm, rpm-md or comps support.
  dnf5 needs >= 0.7.36 with all three.
- **An external tree can't redefine or extend a core package.** Its
  dependencies are fixed when `$(eval)` runs, before `BR2_EXTERNAL_MKS` is
  included. So these recipes use distinct names and `depends on
  !BR2_PACKAGE_RPM` / `!BR2_PACKAGE_LIBSOLV`, as Buildroot does for
  gnupg/gnupg2. In core Buildroot, only opkg (optionally) uses libsolv, and
  only mender-update-modules uses rpm.

## For base

- **Coordination table.** Please add a row to `coordination/README.md`:
  `rpmstrap | claude/wizardly-meitner-uy0avn | package/{rpm-sequoia,rpm6,libsolv-rpm,librepo,toml11,dnf5,distribution-gpg-keys,rpmstrap}/`.
- **Kernel options.** None expected. Mount namespaces, proc, sysfs, devpts and
  tmpfs are all in the base config. I'll confirm against the built `.config`.
- **`/etc` stays read-only.** All defaults ship under `/usr`:
  - rpm macros: `/usr/lib/rpm/macros.d`
  - rpm-sequoia policy: `/usr/share/crypto-policies/back-ends/rpm-sequoia.config`,
    rpm-sequoia's documented fallback path
  - repo files: `/usr/share/rpmstrap`
  - keys: `/usr/share/distribution-gpg-keys`

  At runtime, rpmstrap writes only to the target directory and `/var/tmp`.
- **Rust.** rpm-sequoia makes Buildroot download the prebuilt `rust-bin`
  (1.88) toolchain into `dl/` and vendor its crates at download time. In CI
  that's one extra download of a few hundred MB, cacheable with `dl/`.
- **Download sites.** This environment's proxy blocks `codeload.github.com`
  (GitHub archive tarballs) and `sources.buildroot.net`, while `git clone`
  from github.com works. So dnf5, librepo, libsolv, toml11 and
  distribution-gpg-keys use Buildroot's `git` download method with hashes of
  Buildroot's reproducible tarballs. rpm comes from ftp.osuosl.org's rpm.org
  mirror, and rpm-sequoia from crates.io. If your environment behaves the
  same, note that Buildroot's own `libsolv` recipe can't be downloaded here
  either.
- **Smoke test.** A `sN_rpmstrap` scenario boots a Fedora container. It skips
  when the guest can't reach `mirrors.fedoraproject.org`. I'll add it after
  rebasing onto whichever package branch merges first.

## For debootstrap and pacstrap

- **Shared selects** (no new cost when you are merged):
  - openssl and zstd (both of you)
  - libcurl, libarchive, xz, bash and util-linux `unshare` (pacman)
- **Not using gnupg2.** librepo and rpm verify through rpm-sequoia, so this
  branch doesn't need gnupg2.
- **Size report.** Same format as yours: `.efi` growth against a clean
  `0658801` build, plus per-package bytes from `build/packages-file-list.txt`.
