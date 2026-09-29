# base: status

Branch: `claude/awesome-carson-xsy846`

## Status

- Integration tip: see `git log origin/claude/awesome-carson-xsy846`.
- Base with systemd running from the cpio initramfs: pushed; a full build and
  `make test` are running.
- Merged package branches: none yet.

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
