# Working on Cherry

Cherry is a Buildroot-based Linux distribution: one EFI binary, network-booted
over UEFI HTTP boot, with just enough OS to run systemd-nspawn containers, and
an AI agent (OpenCode, behind OpenChamber) on every machine to run them. This
file is for agents and people changing the repository: how it is put together,
what a change must keep, how to verify one, and how it gets in. What Cherry
does is in [README.md](README.md); this file doesn't repeat it. Claude Code
reads it through `CLAUDE.md`, which only imports it.

## Read first

1. **[README.md](README.md)**, all of it. It is the specification as much as
   the manual: the checks under `tests/` test "what the README says", and a
   change in behavior is incomplete until the README says the new behavior.
2. **The roadmap,** issue [#4](https://github.com/tiltbob/cherry/issues/4),
   and its open phase issues: [#7](https://github.com/tiltbob/cherry/issues/7)
   containers and [#10](https://github.com/tiltbob/cherry/issues/10)
   hardening. They record
   the principles (one image for every machine; the TPM is the identity; no
   central config or enrollment; images never leave the node that built them;
   every trusted key is baked in at build time; systemd's own mechanisms over
   daemons of Cherry's own), the decisions, and what was dropped. Don't reopen
   a decision in a pull request; raise it on the issue.

## The repository

A `BR2_EXTERNAL` tree for Buildroot, which is a git submodule in `buildroot/`
(empty until `make`, or `git submodule update --init --depth 1 buildroot`,
checks it out).

| Path | What it holds |
|---|---|
| `Makefile` | the wrapper around Buildroot: `make`, `make check`, `make qemu`, `make test`, `make sdk`, `make br-<target>`; `make help` lists them |
| `external.mk`, `external.desc`, `Config.in` | the tree's glue: `external.mk` includes every `package/*/*.mk` and adjusts options of Buildroot's own packages; `Config.in` has one `source` line per package, sorted |
| `configs/cherry_x86_64_defconfig` | the image's configuration; `make savedefconfig` writes it back here |
| `configs/cherry_sdk_x86_64_defconfig` | the cross toolchain alone, which `make sdk` builds and the `sdk` workflow publishes as a release; the main defconfig downloads that release as its external toolchain |
| `board/x86_64/` | `post-build.sh`, `post-fakeroot.sh`, `post-image.sh` (os-release, `/root` on `/var`, the initramfs and the UKI, with its signed PCR policy); `linux.fragment` and `check-kconfig.sh`; `rootfs-overlay/` (the image's `/init`, Cherry's units and presets, the firewall, the no-TPM script); `secureboot/` (the public half of the Secure Boot keys and of the PCR key, and `dbx.auth`, the revoked release keys, once there is one) |
| `package/<name>/` | one Buildroot package each: `Config.in`, `<name>.mk`, `<name>.hash`, and the files it installs (units, scripts, rules). Cherry's own code is in `cherry`, `cherry-pit`, `cherry-secureboot` and `rpmstrap`; `opencode` and `bun` are upstream's prebuilt binaries; `openchamber` is an npm tree, listed in `npm-modules.list` |
| `patches/` | `BR2_GLOBAL_PATCH_DIR`: patches to Buildroot's packages, by name and version, and the external toolchain's hash |
| `scripts/` | host-side tools: `run_qemu.py`, `update_packages.py`, `prune_dl.py`, `secureboot.py`, `pcrsign.py`; each one's docstring is its manual |
| `tests/` | the checks that `make check` runs, one file each, and `smoke.py`, the end-to-end test in QEMU |
| `.github/workflows/` | `build.yml` (checks, build, smoke test, image releases) and `sdk.yml` (toolchain releases) |
| `output/`, `dl/`, `.ccache/` | build output, downloads and ccache: ignored, never committed |

Every script, unit, test and workflow starts with a comment that says what it
is, what runs it and what it needs. Read that before the code.

## What a change must keep

- **One image, identical on every machine.** Nothing machine-specific is
  built in. A machine takes what it finds: its TPM, firmware in Secure Boot
  setup mode. Its disks, only the agent claims, with the user's yes: nothing
  touches a disk at boot. No central config, no enrollment, and no control
  plane beyond HTTP and DHCP.
- **The root is read-only, `/etc` included.** Users and groups are created
  at build time with Buildroot's `<PKG>_USERS`; nothing can add one at boot.
  Whatever a service writes goes under `/var` (`StateDirectory=`,
  `LogsDirectory=`); `/root` is a symlink into `/var`; files the image
  carries under `/var` are factory defaults, copied in at boot. `/var` is
  the pool `cherry`'s volume `var`, 128 MiB in RAM until the agent gives the
  pool disks: nothing that runs before that may need more.
- **Everything is in RAM.** The README records the sizes (Memory, OpenCode,
  OpenChamber); update them when a change moves them, and leave out what a
  container host doesn't need. `post-image.sh` prints the UKI's size.
- **The `cherry` user is the agent, and it is unprivileged.** The agent's
  tools are the MCP servers' verbs alone: `/etc/opencode/opencode.json`
  denies OpenCode's own (a shell, files, the web), and the `machines` verbs
  take a name and a profile a person wrote, or a name and an image to clone,
  never a package list, a file or a script. The user's only
  privileges are `package/cherry/50-cherry.rules` (machined's verbs on
  containers, and starting `systemd-nspawn@`, `cherry-bootstrap@`,
  `cherry-image@` and `cherry-storage@` units) and what those three template
  units do as root for it, with their instances checked. `cherry-storage@`
  erases the disks it claims: only a disk nothing uses, and only as it was
  listed (its fingerprint). `cherry-autostart.service` starts for it at boot,
  from its records, what it may start itself, and nothing else. Never give it
  a host path to read or write as root, nor a unit that does;
  `tests/polkit.js` pins the rule to the README's list. Its services listen on loopback only, under system call
  filters that systemd enforces (it is built with seccomp).
- **The firewall owns one table,** `inet cherry`; networkd and nspawn keep
  `io.systemd.nat`. What comes in is the README's list, and nothing more:
  `tests/firewall.sh` and the smoke test pin it.
- **A TPM 2.0 is mandatory.** The machine ID and hostname come from its
  endorsement key, in `/init`, before systemd starts. Without one, boot stops
  at `cherry-no-tpm.target`.
- **Two images from one build, and no SSH.** `cherry-x86_64.efi`, the dev
  image, has root on the console for `make qemu` and the smoke test;
  `cherry-x86_64-prod.efi`, the prod image, which releases are, has root
  locked out by `cherry.prod` on its command line (`rootfs-overlay/init`),
  and nothing else differs. Neither has sshd: a machine is managed through
  OpenChamber, paired from its console. Nothing may give prod a way in that
  the README doesn't list.
- **The disks' keys are sealed to the boot state:** PCR 7, and PCR 11 under
  the image's signed policy (`scripts/pcrsign.py`, which `post-image.sh` signs
  it with; `cherry-pit` hands the signature and key to `/run/systemd`). Every
  path in `cherry-storage` that seals or unlocks a key passes that policy,
  and reseals a key sealed another way; smoke scenario 15 proves that a
  changed or re-signed image unlocks nothing. A release is signed with the
  repository's PCR key, in the release job, so that an update still unlocks
  the disks.
- **The Secure Boot keys are real.** `board/x86_64/secureboot/` is the
  committed public half of keys that live on a Nitrokey HSM 2 and in the
  `release` environment's secret. Never regenerate, edit or "fix" them, and
  write `dbx.auth` through `scripts/secureboot.py revoke` alone;
  `scripts/secureboot.py check` verifies them. Local builds are unsigned,
  unless the build tree has a local chain of its own (README, Secure Boot),
  and a machine with Cherry's keys and Secure Boot on refuses them either
  way.
- **Every download is pinned.** `BR2_DOWNLOAD_FORCE_CHECK_HASHES` is on:
  every `.hash` holds real hashes, each with a comment saying where it came
  from (upstream's checksum or signature, the npm registry's integrity field,
  or "locally computed"), and every package names its license and license
  files.
- **Prebuilt binaries are the "baseline" x86_64 builds,** without AVX2, so
  they run on any VM CPU model. `opencode` is a Bun single-file executable
  that stripping breaks: `BR2_STRIP_EXCLUDE_FILES` lists it and
  `post-build.sh` checks that it survived.
- **Kernel options live in `board/x86_64/linux.fragment`,** and
  `check-kconfig.sh` fails the build when one doesn't survive into the final
  `.config`. List only symbols this kernel has, each under a comment saying
  what needs it. The kernel is locked down (integrity mode) and loads only
  the modules the build signed: a driver the image needs is built in, or a
  module of this kernel's build; nothing loads one from elsewhere.
- **systemd's own mechanisms** (credentials, repart, cryptsetup, machined,
  `.nspawn` files, template units, presets) rather than daemons or formats of
  Cherry's own.
- **QEMU never gets pvpanic** (`scripts/run_qemu.py`): it would exit on a
  kernel panic instead of resetting.

## Verifying a change

### `make check`: the checks that need no build

Each check is one file under `tests/`, or a command of `scripts/secureboot.py`,
prints one line when it passes and fails loudly otherwise. `make check` runs
them all, `make -k check` goes on after a failure, `make check-<name>` runs one.

| Check | What it does | Needs |
|---|---|---|
| `package` | Buildroot's `check-package` over `Config.in`, `package/` and `configs/`: the `.mk`, `.hash` and `Config.in` rules, patches, shellcheck on the scripts (`.shellcheckrc` applies) | the submodule, shellcheck, python3-magic |
| `flake8` | `tests/*.py` and `scripts/*.py`, 120 columns | flake8, as a module of python3 |
| `yaml` | the workflows parse | python3-yaml |
| `syntax` | every `#!` script under `package/` and `board/` parses in its shell; `tests/*.sh` pass shellcheck | shellcheck |
| `units` | `systemd-analyze verify` of every unit, against a root laid out as the image (`tests/units.sh`) | systemd |
| `scenarios` | the smoke test's scenarios are numbered 1, 2, … and its docstring lists them | |
| `polkit` | `50-cherry.rules` allows the `cherry` user what the README says, nothing more | node |
| `bootstrap` | `cherry-bootstrap` checks its instance and calls the tools right, with stand-ins | |
| `image` | `cherry-image` builds from a recipe, in a busybox root under `unshare -r` | static busybox, user namespaces |
| `mcp` | the `machines` MCP server, against stand-ins for systemctl, machinectl, busctl and ip; the `storage` one, against a stand-in systemctl | node |
| `extension` | OpenChamber's Environments extension: its manifest, as the host inspects a package, and its service, against the `machines` stand-ins | node |
| `autostart` | `cherry-autostart` starts the environments whose record enables them, at boot, and nothing else, against a stand-in systemctl | |
| `postbuild` | `post-build.sh` still catches a stripped `opencode` | |
| `firewall` | the rules, in network namespaces standing in for the host, its LAN and its containers | nft, ip, busybox, user namespaces |
| `secureboot` | the keys in `board/x86_64/secureboot/` verify, and `cherry-secureboot` enrolls them as it should, and sets dbx from `dbx.auth` | openssl |
| `ceremony` | `scripts/secureboot.py init`, `issue` and `revoke`, on a SoftHSM token | softhsm2, opensc |
| `storage` | `cherry-storage` claims only the disks it may, drives LVM as it should, seals the keys to the boot state, and gives `/var` the pool `cherry`, in RAM at boot and moved onto its disks by `extend`, against a stand-in sysfs and stand-ins for LVM (a small model of it) and the disk tools | python3, flock |
| `pcrsign` | `scripts/pcrsign.py` measures, signs and checks a UKI's PCR 11 policy as systemd does: on PE binaries the test makes, against systemd's vectors | openssl |
| `version` | the image is named after the nearest `v*` tag | git |

A check whose tool is missing says `SKIPPED` and passes (`polkit`, `mcp`,
`extension`, `image`, `firewall`, `ceremony`); `package`, `flake8` and
`syntax` fail without theirs. CI installs all of them (`build.yml`, "Install
check tools"). An agent sandbox usually has python3, node, make, git, systemd
and openssl, and lacks shellcheck, busybox, `ip`, softhsm2, opensc and the
submodule: then `make -k check` runs thirteen checks, skips three and fails
three for want of tools. Get the
submodule with `git submodule update --init --depth 1 buildroot` and install
what you can; in the pull request, say which checks ran.

### The build and the smoke test

`make` needs a Buildroot host ([README: Building](README.md#building)), about
20 GB of disk, and one to two hours from cold, or half an hour with `main`'s
ccache. `make test` boots the result in QEMU with OVMF and swtpm
([README: Booting](README.md#booting)); its sixteen scenarios take about
eight minutes under KVM, and hours without (the timeouts scale by
`CHERRY_TEST_TIMEOUT_MULT`, 4 without KVM). The last one boots the prod
image, and can only watch its console: root is locked out of it.
Scenarios that need a distribution mirror probe for it first and skip when the
guest can't reach it.

CI does both on every pull request, with KVM. Most agent sandboxes can't: no
KVM, often no QEMU, and a cold build takes hours. Don't start a full build
unless the task needs it; rely on the pull request's `build` check, and say
so in the description.

With a configured `output/` (after `make`, or `make cherry_x86_64_defconfig`),
one package at a time:

- `make br-<pkg>-source`, `-extract`, `-build`, `-rebuild`, `-reinstall`
- `make br-printvars VARS=<NAME>`, a Buildroot variable's value
- `make savedefconfig` after `make menuconfig`: the defconfig must come back
  as you meant it, with the new option where Buildroot puts it
- `buildroot/utils/check-package -b Config.in package/<name>/*`, one package's
  lint

Reading a CI failure:

- `changes` job, step "Check": the failing check's own message, above
  `make: *** [Makefile:…: check-<name>] Error 1`.
- `build` job, step "Build": Buildroot names the package in its last `make`
  error (`…/build/<pkg>-<version>/.stamp_built`); `check-kconfig` and
  `post-build` failures come at the end, after the packages.
- `build` job, step "Smoke test": one `=== N. passed`, `=== N. skipped` or
  `FAILED: <message>` per scenario. The run's artifact, `cherry-x86_64`, holds
  both images and `serial.log`, the VM's console across every boot of the
  run: the first place to look. To run the smoke test against them without
  building, put `cherry-x86_64.efi` and `cherry-x86_64-prod.efi` in a
  directory and run `python3 tests/smoke.py --images <dir> --state <dir>/test`.

## How a change gets in

- **A branch and a pull request against `main`.** `main` changes only through
  pull requests; other branches build only through one, or by hand.
- **CI is the `build` workflow,** a required check:
  - the `changes` job runs `make check` first, in about a minute;
  - the `build` job builds the image and runs the smoke test; it is skipped,
    and counts as passed, when the pull request changes only Markdown, the
    checks' own test files or the scripts the build never runs (the two lists
    in `build.yml`, which must agree);
  - the `release` job runs only for releases, from `main` or a `v*` tag.
- **Commit messages** say what changed and why. The subject names the area,
  then the change: `cherry-image: drop the host's /etc/hostname that
  debootstrap copies`; `smoke: have the kernel re-read the wiped disk's
  partition table`; `build: read the tag check's output, and keep no token
  after checkout`. The body gives the symptom with its evidence (the CI run
  number, the log line, the issue), the cause, what the change does, and
  which tests cover it. End with the trailers: `Co-Authored-By:` as your
  harness gives it, then `Claude-Session:` (Attribution, below).
- **Pull request descriptions** have the same shape: why; what changes, by
  file or unit; the tests; what ran locally and what didn't. Say plainly what
  you couldn't run ("no KVM here; this PR's `build` check runs the smoke
  test"). Open it ready for review, with `Claude-Session:` on a line of its
  own at the end.
- **Keep a branch current by merging `main` into it,** never by rewriting
  pushed history: a merge commit, `Merge main: <what it brings>`, whose body
  says how each conflict was resolved. Pull requests merge with a merge
  commit too: the title, a blank line, the trailer.
- **One subject per pull request.** A failure that isn't the pull request's
  (a scenario fixed on `main` since) comes in by merging `main`, or as that
  fix's commit cherry-picked and named as such, so it merges as a no-op.
- **Everything a behavior touches moves in the same change:** the code, the
  comments of the unit or script, the check under `tests/`, the smoke
  scenario, the package's `Config.in` help, and the README section. A new
  test file is also listed in `build.yml`'s two skip lists, the Makefile's
  `CHECKS` and its comment, and the README's `make check` paragraph.
- **Releases** are cut by hand, from `main`, by the `build` workflow given a
  name `v<version>`; SDK releases by the `sdk` workflow, `sdk-YYYY.MM.DD.NNN`.
  Both are in [README: Building](README.md#building). An agent doesn't cut
  them.

### Attribution: name the session

Claude sessions act on GitHub as the account that connected them, so the
author shown on a commit, pull request, comment or issue doesn't say which
session wrote it. Name the session instead, by the URL of its
`Claude-Session:` commit trailer: `https://claude.ai/code/session_…`.

- **Commits:** end the message with the `Claude-Session:` trailer. That
  includes merge commits made through the GitHub API: pass a commit message,
  the pull request's title, then a blank line and the trailer, rather than
  GitHub's default.
- **Pull request descriptions, comments, reviews, review replies and
  issues:** put `Claude-Session: <url>` on a line of its own at the end of the
  body, just before the "Generated by Claude Code" footer if there is one.
- **One session per item:** each commit, comment or issue names the session
  that wrote it. A pull request can collect work from several sessions.

The trailer makes the history searchable:

```sh
# everything one session committed
git log --grep=session_<id>
# commits per session
git log --no-merges --format='%(trailers:key=Claude-Session,valueonly)' | sort | uniq -c
```

The line is a claim, not proof: anyone who can push or comment can write it.

## Recipes

### A new package

1. `package/<name>/Config.in`, `<name>.mk` and `<name>.hash`, in Buildroot's
   format: `depends on` for what the toolchain and the image must provide,
   `select` for its dependencies, each with a comment saying why (`# bash`,
   `# cherry-image: mv -T`); a help text that says what it is for in Cherry.
2. One `source` line in `Config.in`, in sorted order.
3. Enable it in the defconfig: `make menuconfig`, then `make savedefconfig`.
4. A unit: install it from `<PKG>_INSTALL_INIT_SYSTEMD` with one line per
   file, in exactly this form, which `tests/units.sh` parses to lay out its
   root:
   `$(INSTALL) -D -m 0644 $(<PKG>_PKGDIR)/<unit> $(TARGET_DIR)/usr/lib/systemd/system/<unit>`.
   Its `ExecStart=` must name the path the package installs its script at.
   A unit that should not run by default is disabled in
   `board/x86_64/rootfs-overlay/usr/lib/systemd/system-preset/10-cherry.preset`.
5. A user or group: `<PKG>_USERS`.
6. `make check-package check-units`, then a build and `make test`, or CI.
7. The README: a section, or a line in the one that covers it, and the sizes
   if they moved.

A prebuilt binary (`opencode`, `bun`) takes upstream's x86_64 "baseline"
build, with its license fetched as an extra download and hashed. An npm
package (`openchamber`) lists its dependency tarballs in `npm-modules.list`,
which `scripts/update_packages.py` generates from npm's lockfile; the build
needs neither node nor npm.

### A kernel option

Add it to `board/x86_64/linux.fragment` under a comment saying what needs it.
`check-kconfig.sh` fails the build if the option, or an `is not set` line,
doesn't survive into `.config`, which happens silently when a dependency is
unmet: check the symbol's dependencies for this kernel version first.

### A unit or script of the overlay

`board/x86_64/rootfs-overlay/` is copied onto the root as it is. Cherry's
units are `cherry-*.service` and `cherry-no-tpm.target`; their scripts live
under `usr/lib/cherry/`. Order against systemd's targets explicitly (the
firewall before `network-pre.target`; `cherry-storage.service`, of
`package/cherry/`, before `var.mount`), condition on the TPM where the README
says (`cherry-storage.service`:
`ConditionPathExists=/run/cherry/identity/ek-hash`), and give each unit the
hardening its neighbors have.
`make check-units` verifies the result; a change in what a unit does is also a
change to its script's test under `tests/` and to the README.

### A smoke scenario

- A method `s<N>_<name>` of `Smoke` in `tests/smoke.py`, numbered after the
  last one with no gap, and its line in the module docstring under the same
  number (`  N. …`); `make check-scenarios` compares the two.
- Boot with `self.boot()`: the TPM interface, disks, Secure Boot, another
  image server, the expected system state. It stops the previous VM; the
  TPM's state lasts for the whole test, so the VM stays the same machine.
- Drive the VM with `vm.run()`, which fails on a nonzero exit unless told
  `check_rc=False`; assert with `check(condition, message)`, whose message
  names what was found; report with `log()`. Timeouts scale with `MULT`.
- A scenario that needs a mirror or the internet probes for it first and
  returns `SKIPPED`, with the reason logged, as `s5_debootstrap` does.
- Run as the `cherry` user (`AS_CHERRY`), inside `cherry.service`'s sandbox
  where the agent would (`IN_CHERRY_SERVICE`), whenever the README says that
  user can do it: the scenario is what proves the privileges are enough and
  no more.
- Clean up what the scenario made, so the next one starts as the README
  describes a machine.

### A new check

A file under `tests/` with a header comment saying what it checks, how it
stands in for the real thing, and what it needs; it prints one line on
success and skips, saying so, when its tools are missing. Then a `check-<name>`
target in the Makefile, in `CHECKS` and its comment; the file in both skip
lists of `build.yml`; and the README's `make check` paragraph.

### Bumping packages

`scripts/update_packages.py` finds the newest upstream releases and rewrites
the `.mk` versions and `.hash` files, through Buildroot's own download
(`--check` only reports; `name=version` picks one). It needs network, git and,
for OpenChamber, npm. Then review the diff (a `.hash` comment that claims a
checked signature needs that check again), build, run `make test`, and update
the README's sizes.

### Changing the toolchain

Edit `configs/cherry_sdk_x86_64_defconfig`, publish an SDK release
(`sdk-YYYY.MM.DD.NNN`, through the `sdk` workflow), then point the main
defconfig's `BR2_TOOLCHAIN_EXTERNAL_URL` and
`patches/toolchain-external-custom/toolchain-external-custom.hash` at it. The
two defconfigs' toolchain options must stay the same.

## Style

- **Shell:** `#!/bin/sh` and POSIX sh, unless the shebang says bash
  (`rpmstrap`); `set -eu`; tabs; shellcheck clean (`.shellcheckrc` disables
  SC2013 alone, with the reason). Scripts the image runs are in
  `package/<name>/`, installed to `/usr/libexec` or `/usr/bin`, or in the
  overlay's `usr/lib/cherry/`.
- **Python:** the standard library only (`smoke.py` and `run_qemu.py` run on
  a bare host); flake8 at 120 columns; four spaces; a module docstring that
  is the tool's manual, which `--help` prints.
- **JavaScript:** runs on Node without dependencies, and on Bun on the image;
  `"use strict"`; two spaces. The extension's panel runs in OpenChamber's
  sandboxed iframe as classic scripts, with no bundler: it speaks the SDK's
  postMessage protocol itself (`openchamber-extension/panel/main.js`).
- **Makefiles and Kconfig:** Buildroot's conventions, as `check-package`
  enforces them.
- **Comments say why, and cite:** the upstream file and version a workaround
  is for, the CI run a failure showed in, the README section a rule comes
  from. A test's header says what it checks, what stands in for what, and
  what it needs.
- **Prose** (README, help texts, commit messages, pull requests): plain and
  exact. Say what a thing does and why, name the files and commands, give the
  numbers, and don't hedge or sell. Wrap Markdown at about 80 columns. The
  README is the model; match its voice in new sections.

## Where things are

| Feature | Code | Checks | README |
|---|---|---|---|
| Boot, initramfs, UKI | `package/cherry-pit/`, `board/x86_64/post-image.sh`, `rootfs-overlay/init`, `linux.fragment` | s1 | How it starts |
| Machine identity, no TPM | `rootfs-overlay/init`, `cherry-no-tpm.{service,target}`, `usr/lib/cherry/no-tpm` | s1, s4, s12 | Machine identity |
| The dev and prod images | `board/x86_64/post-image.sh`, `rootfs-overlay/init` (`cherry.prod`), `scripts/run_qemu.py --prod` | s16 | Building |
| `/var` on the pool `cherry`, in RAM until it has disks | `package/cherry/cherry-storage` (`boot start`), `cherry-storage.service`, `lvm.conf`; the overlay's `var.mount.d/` | `tests/storage.py`, s1, s2, s4 | Disks |
| The disks' keys sealed to the boot state | `scripts/pcrsign.py`, `board/x86_64/post-image.sh`, `package/cherry-pit/src/cherry-pit.c`, `package/cherry/cherry-storage`, `scripts/secureboot.py pcr`, the `release` job of `build.yml` | `tests/pcrsign.py`, `tests/storage.py`, `tests/ceremony.sh`, s2, s4, s15 | Disks, Secure Boot |
| Swap in RAM | `cherry-zram-swap.service`, `usr/lib/sysctl.d/50-cherry-zram.conf` | s11 | Memory |
| Firewall | `usr/lib/cherry/firewall.nft`, `cherry-firewall.service` | `tests/firewall.sh`, s1, s3 | Firewall |
| Secure Boot | `package/cherry-secureboot/`, `board/x86_64/secureboot/`, `scripts/secureboot.py`, the `release` job of `build.yml`, `post-image.sh` (a local chain's signature) | `tests/secureboot.sh`, `tests/ceremony.sh`, `secureboot.py check`, s13 | Secure Boot |
| Kernel lockdown, signed modules | `board/x86_64/linux.fragment`, `check-kconfig.sh` | s1 | Secure Boot |
| Containers from the host's tools | `package/debootstrap/`, `package/ubuntu-keyring/`, `package/rpmstrap/`, `package/pacman/`, `package/arch-install-scripts/`, `package/cherry/cherry-bootstrap` | `tests/bootstrap.sh`, s5, s6, s7 | Running containers |
| Images from recipes; environments from profiles, the agent's tools and OpenChamber's extension; the enabled ones started at boot | `package/cherry/cherry-image`, `cherry-image-setup`, `profiles/`, `environments.js`, `cherry-machines-mcp.js`, `machine-images.md`, `opencode.json`, `openchamber-extension/`, `openchamber-registry.json`, `cherry-autostart`, `cherry-autostart.service` | `tests/image.sh`, `tests/mcp.js`, `tests/extension.js`, `tests/autostart.sh`, s10 | Building images, Environments |
| Storage pools, the agent's tools | `package/cherry/cherry-storage`, `cherry-storage{,@}.service`, `lvm.conf`, `storage-pv.conf`, `cherry-storage-mcp.js`, `mcp-stdio.js`, `storage.md`, `configure-storage.md`, `manage-storage.md`; `linux.fragment`, `10-cherry.preset` | `tests/storage.py`, `tests/storage-mcp.js`, `tests/polkit.js`, s2, s14 | Storage |
| The `cherry` user and its privileges | `package/cherry/50-cherry.rules`, `cherry.service`, `cherry-connect-url` | `tests/polkit.js`, s9 | As the cherry user, Cherry service |
| OpenCode, OpenChamber, Bun | `package/opencode/`, `package/openchamber/`, `package/bun/` | `tests/postbuild.sh`, s8, s9 | OpenCode, OpenChamber |
| The image's name and version | `Makefile`, `board/x86_64/post-build.sh` | `tests/version.sh`, `tests/postbuild.sh` | Building |
| Toolchain and SDK | `configs/cherry_sdk_x86_64_defconfig`, `patches/toolchain-external-custom/`, `sdk.yml` | the `sdk` workflow's own check | Building |
| QEMU | `scripts/run_qemu.py` | `tests/smoke.py` uses it | Booting |

`s<N>` is a scenario of `tests/smoke.py`; the overlay paths are under
`board/x86_64/rootfs-overlay/`; the units of `package/cherry/` are installed
by `cherry.mk`.
