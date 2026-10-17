---
name: Storage
description: How this machine's storage works - disks Cherry claimed, LVM pools of them with SSDs as cache, volumes - and the storage tools; load it before configure-storage or manage-storage
---

# Storage

This machine runs Cherry. Its storage is LVM, on disks Cherry has claimed.
You cannot touch a disk yourself; the `storage` tools do it for you, as root.
To set the machine's storage up, follow the `configure-storage` skill; to
change it, `manage-storage`.

| Tool | Does |
|---|---|
| `list` | the disks: kind (ssd, hdd), state, fingerprint; the pools and their volumes |
| `claim` | **erases** disks and makes them Cherry's, ready for a pool |
| `release` | **erases** claimed disks that are in no pool, and leaves them blank |
| `create_pool` | a pool of claimed disks: data disks, and SSDs as its cache |
| `add_disks` | more data disks for a pool |
| `set_cache` | a pool's cache disks and cache mode |
| `remove_disk` | a disk out of a pool, its data moved off first |
| `destroy_pool` | a pool that has no volumes |
| `create_volume` / `resize_volume` / `remove_volume` | volumes, mounted for you |

## How it fits together

- **Disks** as `list` shows them, by state:
  - `free`: not in use; `claim` may take it. `blank`, or with what is on
    it: its partitions and filesystems, which claiming erases.
  - `claimed`: Cherry's, in a pool as `data` or `cache`, or in none yet.
  - `locked`: claimed by another machine, or by this one before its TPM was
    cleared. Its data is unreadable here, and `claim` may take it, erasing it
    for good.
  - `in-use`: mounted, swap, held by something else, or this machine's
    `/var` disk. Not to be claimed.
  - `unsuitable`: removable, read-only, on USB, or under 1 GiB.

  `kind` is what the kernel reports: an SSD behind a RAID controller, or a
  virtual disk, may show as `hdd`; if the user says otherwise, go by the
  user. Each disk's `fingerprint` changes with its partition table: `claim`
  and `release` take it, and refuse a disk that changed since it was listed,
  or that a reboot renamed.
- **A claimed disk** is erased, then made one LUKS2 volume that this
  machine's TPM unlocks, holding an LVM physical volume: unreadable on any
  other machine.
- **A pool** is an LVM volume group of claimed disks. Its **data disks** hold
  the volumes, one after the other: the pool's size is theirs added up, with
  no redundancy, so a data disk that dies takes with it the volumes that had
  data on it. Its **cache disks**, SSDs, cache every volume (dm-cache): each
  volume gets a share of the cache as large as its share of the data disks.
  The cache is **writethrough** unless the user chose **writeback**: faster
  writes, but a cache disk that dies loses what it had not written back yet.
- **A volume** is a logical volume on the pool's data disks, holding ext4
  that you own, mounted on `/var/lib/cherry/volumes/<pool>/<volume>` now and
  at every boot. Work in it as in any directory of yours. Volumes grow but
  do not shrink.

Storage here is always LVM: these tools make nothing else.

## Rules

- Never erase without the user's yes to exactly what goes: `claim` and
  `release` erase disks, `remove_volume` a volume's data, `destroy_pool` the
  pool. Name each disk (name, size, model, serial, what is on it) or volume.
- Claim only the disks the user chose, with the fingerprints from the last
  `list`. Refused because a disk is not as listed: `list` again, and check
  with the user before going on.
- One storage operation at a time: another one fails until the first is
  done, so wait and try again. Moving a disk's data (`remove_disk`) can take
  hours.

## Good to know

- **/var:** containers, their images and your home, `/var/lib/cherry`, live
  on `/var`: this machine's encrypted disk if it has one (`list` shows `var`
  on `/dev/mapper/cherry-var`, and that disk in use), otherwise in RAM
  (`tmpfs`), where nothing lasts past a reboot. A pool never holds `/var`. A
  machine without a `/var` disk takes its first blank disk at boot: to give it
  one, leave a disk out of the claim (or `release` one, which leaves it
  blank), and have the machine rebooted.
- **Volumes last** across reboots even when `/var` is in RAM: they are on the
  pool's disks, mounted again at boot.
- **Containers** cannot use volumes yet: there is no way to bind one into a
  container. Volumes are for your own files: projects, data, caches.
- **No TPM, no claims:** the TPM holds the claimed disks' keys.
