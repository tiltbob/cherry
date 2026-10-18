---
name: Manage storage
description: Change this machine's storage - grow a pool, change its SSD cache, take out or replace a disk, make, grow or remove volumes, destroy a pool, give claimed disks back
---

# Manage storage

Load the `storage` skill first, if you have not: what claimed disks, pools
and volumes are, the `storage` tools, and their rules. Start with `list`, and
before anything that erases data, ask the user, naming exactly what goes.

- **Grow a pool:** `claim` the new disk (the user chooses it, as when
  configuring), then `add_disks` it to the pool. A new SSD goes to the cache
  instead: `set_cache`.
- **The cache:** `set_cache` with all the cache disks, old and new, or none,
  and the mode. Every volume's cache is made anew, so the caches start cold;
  a writeback cache is written back first.
- **Take a disk out:** `remove_disk`. A data disk's data moves to the pool's
  other data disks, which need room for it (see `list`: the pool's `free`
  must be at least what the disk holds); copying a full multi-terabyte disk
  takes hours. Not the pool's last data disk: destroy the pool instead, or,
  for `cherry`, which holds `/var`, keep it. The disk stays claimed:
  `release` it if the user wants it erased and given back.
- **Replace a disk** that is failing, while it still reads: `claim` the new
  one, `add_disks` it, then `remove_disk` the old one, whose data moves to
  the new.
- **Volumes:** `create_volume` with a name and a size (`500G`, `2T`, or `all`
  for the pool's free space); `resize_volume` grows it, while in use, and
  never shrinks it; `remove_volume` deletes it and its data, and fails while
  anything has files open in it.
- **`/var`** is `cherry`/`var`: `resize_volume` grows it like any volume, and
  `/var` with it. It is never removed, the pool `cherry` never destroyed, and
  its last data disk stays.
- **Destroy a pool:** `remove_volume` each of its volumes, then
  `destroy_pool`. Its disks stay claimed, to make another pool of, or to
  `release`.
- **Give disks back:** `release` erases claimed disks that are in no pool and
  leaves them blank.

`list` shows a pool's `missing_disks` when one of its disks is gone: tell
the user. Volumes with data on it are not mounted, and its other volumes
still are. A dead cache disk takes the cached volumes with it until it is
detached, which these tools cannot do yet: that is `lvconvert --uncache
--force`, by root.
