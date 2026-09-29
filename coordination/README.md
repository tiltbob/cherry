# Coordination between Cherry sessions

Several sessions work on Cherry in parallel, each on its own branch. They
coordinate only through git: this directory, and branches pushed to `origin`.

| Session | Branch | Owns |
|---|---|---|
| base | `claude/awesome-carson-xsy846` (integration branch) | `board/`, `linux.fragment`, `post-*.sh`, `Makefile`, CI, `tests/smoke.py` structure, `Config.in` layout |
| debootstrap | `claude/eager-ritchie-5q9d1f` | `package/debootstrap/` (plus its dependencies) |
| pacstrap | `claude/jolly-ritchie-cw9f5o` | `package/pacman/`, `package/arch-install-scripts/` (plus dependencies) |

## Protocol

1. **Base.** Start from the tip of `claude/awesome-carson-xsy846` and rebase
   onto it before every push (`git fetch origin claude/awesome-carson-xsy846 &&
   git rebase origin/claude/awesome-carson-xsy846`).
2. **Your status file.** Each session writes only its own file,
   `coordination/<session>.md`: status, the sha you want merged, kernel options
   you need, the rootfs size you add, and questions. Nobody edits another
   session's file, so this directory never conflicts.
3. **Messages to another session.** Add them to your own file, under a
   `## For <session>` heading. Read the other files after every fetch.
4. **Ready to merge.** Push your branch with status `ready` and the sha. The
   base session watches `origin` and does the rest:
   - merges it into the integration branch with a merge commit
   - rebuilds and runs `make test`
   - pushes, and updates `coordination/base.md` with the outcome
5. **Rebase after each merge.** Once one package branch is merged, the other
   rebases onto the new integration tip before asking for its own merge.

## Integration rules for package branches

- **Recipes.** Put them in `package/<name>/{Config.in,<name>.mk,<name>.hash}`
  with real sha256 hashes (`BR2_DOWNLOAD_FORCE_CHECK_HASHES=y`).
- **`Config.in`.** Add exactly one sorted `source` line per package inside
  the menu.
- **Defconfig.** Enable the package in `configs/cherry_x86_64_defconfig`, then
  run `make savedefconfig`.
- **Kernel options.** Don't edit `board/` or the fragment. List the options in
  your status file and the base session adds them.
- **Smoke test.** You may add your own scenario method to `tests/smoke.py`
  (`sN_<tool>`, appended to `scenarios`). It must skip cleanly when the guest
  has no outbound network (CI and TCG).
- **README.** Add one subsection under "Running containers".

## Runtime constraints of the image

- **`/`.** Buildroot's zstd cpio initramfs inside one UKI
  (`cherry-x86_64.efi`, UEFI HTTP boot). systemd runs from it as PID 1 and
  remounts it read-only. The rootfs lives entirely in RAM on every host, so keep
  dependencies lean.
- **`/var`.** A tmpfs: `/var/lib/machines` is writable but lost on reboot.
- **`/etc`.** Read-only. Ship keyrings and default config under `/usr`, and
  write only to `/var`, `/run` or `/tmp` at runtime.
- **Already on the image.** ca-certificates, nftables, systemd-nspawn and
  machined, busybox, openssh.
