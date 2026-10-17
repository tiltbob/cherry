---
name: Configure storage
description: Set up this machine's storage when asked to configure, set up or add storage on it - ask which disks to claim, propose an LVM pool of them (SSDs as the hard disks' cache), and make it once the user agrees
---

# Configure storage

Load the `storage` skill first, if you have not: what claimed disks, pools
and volumes are, the `storage` tools, and their rules.

Storage here is always LVM. To configure it:

1. **`list`.** Note which disks are `free` or `locked` (those `claim` may
   take), what is on each (`blank`, or its partitions and filesystems, which
   claiming erases), and which are `in-use` or `unsuitable`, and why: this
   machine's `/var` disk is one of the disks in use. Claimed disks in no pool
   are Cherry's already, and need no claim. Note too whether `/var` is on a
   disk or in RAM.
2. **Ask which disks to claim.** Use the question tool, with `multiple: true`
   and one option per disk that `claim` may take: its name as the label, and
   its size, kind, model, serial and what is on it as the description.
   Preselect nothing and never assume all of them. If no disk can be claimed,
   say why and stop.
3. **Propose a configuration** from the kinds of the chosen disks, and of
   claimed disks in no pool if the user wants them in too:
   - **only SSDs:** one pool of all of them, with no cache;
   - **only hard disks:** one pool of all of them, with no cache;
   - **both:** one pool of the hard disks, with the SSDs as its cache,
     writethrough.

   Say plainly:
   - which disks will be **erased**: name, size, model, serial, and what is
     on them now;
   - the pool's name (suggest `data`, or ask), and its size, the data disks
     added up;
   - its cache, the SSDs added up, and the cache mode; writeback is the other
     one, faster on writes, but a cache disk that dies loses what it had not
     written back yet: propose writethrough;
   - that the pool has no redundancy: a data disk that dies takes the
     volumes with data on it;
   - if `/var` is in RAM, that a disk left out and blank becomes the
     machine's `/var` disk at the next boot, so that containers and your
     home persist.

   If the user said what the storage is for, propose a first volume for it
   too: a name, and a size or `all`.
4. **Wait for the user's yes** to that proposal, as claiming erases the
   disks. Change it as they ask: the rules above make the proposal, they do
   not limit what the user may choose.
5. **`claim`** the disks, each with its fingerprint from step 1. A refusal
   because a disk is not as it was listed means it changed, or a reboot
   renamed it: `list` again, and check with the user before going on.
6. **`create_pool`** with the data disks and, if there are SSDs among them,
   the SSDs as `cache`. Then `create_volume` for any volume the user agreed
   to.
7. **Report** what the tools returned: the pool, its size and free space, its
   cache, its volumes and where they are mounted.
