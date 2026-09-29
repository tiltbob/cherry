# rpmstrap: status

Branch: `claude/wizardly-meitner-uy0avn`, rebased onto `c7f7b54`.

## Status

**Not ready yet.** The recipes are pushed for review, and a full `make` with
rpmstrap enabled is running here. Once it finishes, rpmstrap gets a real run
in a chroot of the Buildroot target: Fedora, then an EL9 and an EL10 rebuild.
The size report follows. This environment has no QEMU or KVM, so `make test`,
including the new `s4_rpmstrap`, runs only in CI or in the base session's
integration build.

## What this branch adds

```sh
rpmstrap fedora 44 /var/lib/machines/fedora
machinectl start fedora
```

The tool is upstream **dnf5** with `--installroot`, wrapped by a small
`rpmstrap` script.

| Package | Version | What it is |
|---|---|---|
| `package/rpm-sequoia` | 1.10.3 | rpm's OpenPGP backend, a Rust cdylib. Uses the `crypto-openssl` feature. |
| `package/rpm6` | 6.1.0 | rpm, cmake/C++20. See below for why it isn't Buildroot's `rpm`. |
| `package/libsolv-rpm` | 0.7.40 | libsolv built as Fedora builds it: rpm, rpm-md, comps and every compression. |
| `package/librepo` | 1.21.1 | Metadata downloader. Verifies OpenPGP through rpm (`USE_GPGME=OFF`), so there's no gnupg. |
| `package/toml11` | 4.4.0 | Header-only, staging only. Build dependency of dnf5. |
| `package/dnf5` | 5.4.6.0 | `dnf5` plus `/usr/bin/dnf`. No daemon, CLI plugins, modularity, systemd/sdbus, bindings or docs. |
| `package/distribution-gpg-keys` | 1.123 | Every RPM distribution's signing keys, as used by mock. Installed in `/usr/share/distribution-gpg-keys`, without the 144 MB of copr keys. |
| `package/shadow-useradd` | 4.18.0 | Only `useradd`, `groupadd` and `usermod` from Buildroot's shadow source (see "For base"). |
| `package/rpmstrap` | in-tree | The wrapper, plus one profile directory per distribution in `/usr/share/rpmstrap`. |

**Profiles:**
- `fedora`: 41 and later.
- `centos-stream`, `almalinux` and `rocky`: 9 and 10.

EL8 is refused, because its repositories need modularity and Buildroot has no
libmodulemd.

Each profile holds `*.repo` files, with gpgkeys pointing into
`distribution-gpg-keys`, plus a `profile` file that sets the default package
set and the rpmdb path. The Fedora set follows the systemd-nspawn(1) example
and includes systemd-networkd.

**What `rpmstrap <distro> <release> <dir> [pkg|dnf5-opt]...` does:**
1. Runs dnf5 with `--use-host-config --setopt=reposdir=<profile dir>`, so only
   that distro's repositories load and nothing is read from `/etc`.
2. Sets `%_dbpath` to what the target's own rpm expects. This goes through a
   private `$XDG_CONFIG_HOME/rpm/macros`, a documented rpm lookup path.
   - Fedora and EL10 use `/usr/lib/sysimage/rpm`; EL9 uses `/var/lib/rpm`.
   - Afterwards it asks the target's rpm (`chroot ... rpm --eval %_dbpath`)
     and fails loudly on a mismatch, rather than leave a container whose
     rpm/dnf sees an empty database.
3. Mounts `/proc`, a read-only `/sys`, a minimal `/dev`, `/run` and `/tmp`
   into the root inside a private mount namespace. These are for rpm
   scriptlets, and they vanish with the namespace.
4. Keeps dnf's metadata cache in a temporary directory under `/var/tmp` and
   deletes it afterwards.
5. Uses `install_weak_deps=False`, as the systemd-nspawn(1) example does.
   `--setopt=install_weak_deps=True` overrides it.

**Tree changes:**
- `Config.in`: nine sorted `source` lines.
- defconfig, from `savedefconfig`:
  - `BR2_TOOLCHAIN_BUILDROOT_CXX=y` (see "For base")
  - `BR2_PACKAGE_LUA=y`: rpm's `depends on`, as Buildroot's own rpm has it
  - `BR2_PACKAGE_RPMSTRAP=y`: selects the rest
- `tests/smoke.py`: `s4_rpmstrap`. It skips when the guest can't open a TCP
  connection to `mirrors.fedoraproject.org:443`.
  1. It runs `rpmstrap fedora 44` and boots the container with `machinectl`.
  2. It checks that the container's own rpm finds the packages
     (`systemd-run -M fedora --pipe rpm -q ...`).
  3. It removes the container.
- README: a "RPM-based containers with rpmstrap" subsection. This comes with
  the size numbers.

## Why rpm and libsolv are new recipes rather than Buildroot's

- **Buildroot's `rpm` is 4.18.1**, as it is on Buildroot master.
  - dnf5 5.4 requires rpm >= 4.19.
  - Fedora 42+ packages rely on rpm's native sysusers.d handling (4.19+).
  - rpm >= 4.19 needs rpm-sequoia for OpenPGP. Without it, rpm 6 builds a
    dummy backend that verifies no signatures at all.
- **Buildroot's `libsolv` is 0.7.35** with no rpm, rpm-md or comps support.
  dnf5 needs >= 0.7.36 with all three.
- **An external tree can't redefine or extend a core package.** Its
  dependencies are fixed when `$(eval)` runs, before `BR2_EXTERNAL_MKS` is
  included. So these recipes use distinct names with `depends on
  !BR2_PACKAGE_RPM` / `!BR2_PACKAGE_LIBSOLV`, like gnupg/gnupg2. In core
  Buildroot, only opkg (optionally) uses libsolv, and only
  mender-update-modules uses rpm.

## For base (re: `coordination/base.md` @ c7f7b54)

- **C++ toolchain: `BR2_TOOLCHAIN_BUILDROOT_CXX=y`.** rpm 6 and dnf5 are
  C++20, and the base toolchain had no C++. This rebuilds the toolchain and
  adds libstdc++ to the image. It's part of my size report, and is on my
  branch's defconfig. Neither debootstrap nor pacstrap needs it.
- **sysusers, re: users at build time.** Agreed. rpmstrap adds no host users.
  - rpm still has to create the *target's* sysusers.d users while it
    installs into `/var/lib/machines/<name>`, before unpacking their files.
    Fedora 42+ packages no longer carry `useradd` scriptlets.
  - I had pointed rpm at the host's `systemd-sysusers --root`, which your
    change removes.
  - rpm now uses its upstream default helper, `/usr/lib/rpm/sysusers.sh`
    (bash). It calls `useradd`/`groupadd`/`usermod -R <root>`, which only
    ever write the target's `/etc`.
  - Those three tools come from `package/shadow-useradd`: Buildroot's shadow
    4.18.0 source and hash, installing only those binaries. Selecting
    Buildroot's full `shadow` would replace BusyBox's `login`, `passwd` and
    `nologin` on the host, which is your console login.
- **Coordination table.** Please add a row to `coordination/README.md`:
  `rpmstrap | claude/wizardly-meitner-uy0avn | package/{distribution-gpg-keys,dnf5,librepo,libsolv-rpm,rpm-sequoia,rpm6,rpmstrap,shadow-useradd,toml11}/`.
- **Kernel options.** None expected: mount namespaces, proc, sysfs, devpts
  and tmpfs are all in the base config. I'll confirm against the built
  `.config`.
- **`/etc` stays read-only.**
  - Defaults ship under `/usr`: repo files in `/usr/share/rpmstrap`, keys in
    `/usr/share/distribution-gpg-keys`, and rpm's config in `/usr/lib/rpm`.
  - dnf5 installs its stock `/etc/dnf/dnf.conf` (an empty `[main]`) as a
    read-only default.
  - At runtime, rpmstrap writes only to the target directory and to
    `/var/tmp`.
- **Rust.** rpm-sequoia makes Buildroot download the prebuilt `rust-bin`
  1.88 (218 MB in `dl/`) and vendor crates at download time. In CI that's an
  extra download, cacheable with `dl/`.
- **Download sites.** This proxy blocks `codeload.github.com`, so GitHub
  archive tarballs fail. So dnf5, librepo, libsolv, toml11, rpm-sequoia and
  distribution-gpg-keys use Buildroot's `git` method, with hashes of
  Buildroot's reproducible `-git4`/`-cargo4` tarballs. rpm comes from
  ftp.rpm.org, and shadow from its GitHub release asset. (Correction to my
  first note: `sources.buildroot.net` serves files fine; only its index
  page is 403.)

## For debootstrap and pacstrap

- **Shared selects** (no new cost when you are merged):
  - openssl and zstd (both of you)
  - libcurl, libarchive, xz, bash and util-linux `unshare` (pacman)
- **No gnupg2 here.** rpm and librepo verify through rpm-sequoia.
- **C++ toolchain.** It comes with this branch. It doesn't affect your
  recipes.
- **Size report.** Same format as yours: `.efi` growth against a clean
  `c7f7b54` build, plus per-package bytes from
  `build/packages-file-list.txt`.
- **Smoke `scenarios` list.** Whoever merges later renumbers; mine is
  `s4_rpmstrap` for now.
