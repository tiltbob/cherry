---
name: Machine images
description: Build images from profiles, and create, start, check, stop and remove the environments cloned from them, with the machines tools
---

# Images and environments

This machine runs Cherry, a container host. Two kinds of thing live in
`/var/lib/machines`, managed with machinectl:

- An **image** is built from a profile and never started. It is what
  environments are made from.
- An **environment** is a clone of an image, with changes of its own, which
  you start, check, stop and remove. Several can come from one image.

You cannot write there, run systemd-nspawn, or run commands in a container
yourself; the `machines` tools do what can be done:

| Tool | Does |
|---|---|
| `list` | the images (with their environments), the environments (with their image), other machines, the profiles |
| `build` | builds an image from a profile (minutes) |
| `remove_image` | removes an image that has no environments left |
| `log` | the end of an image's build log |
| `create` | clones an image into a new environment; given a profile instead, builds its image first if there is none |
| `start` / `stop` | boots / shuts down an environment |
| `status` | what a machine reports: its kind, its image or profile, systemd's state, its address, failed units |
| `remove` | removes an environment (its image stays) |

## Profiles

A profile is a distribution with its setup, written by a person: the machine
ships some (`/usr/lib/cherry/profiles/`), and its administrator may add more
(`/var/lib/cherry/profiles/`). `list` gives their ids, titles and
descriptions; pick the one whose distribution the request names, and ask
when none fits. There is no way to add packages, files or a script from
here: that takes a new profile, which only a person can write.

Name an image after its profile unless there is a reason not to: `create`
with a profile looks for an image of that name, and builds it when there is
none. Names are up to 48 letters, digits and `-`, starting with a letter or
digit, and images and environments share them.

## Workflow

1. `list`, to see the images, the environments and the profiles, and
   whether a name is taken.
2. `create` the environment: from an image by name, or from a profile,
   which builds the image first when there is none (minutes). A failed
   build returns the end of its log, and `log` shows more of it.
3. `start` it, then `status`: systemd `running` and an address on `host0`
   mean it is up. `degraded` names failed units.
4. `stop` it when it is done; `remove` it when it is no longer wanted. An
   environment must be stopped before it is removed. An image is removed
   with `remove_image`, or rebuilt with `build` and `replace`, only once it
   has no environments left.

Do not guess: when the request does not say which profile or image, or
names something that `list` does not show, ask.

## Networking

An environment started with `start` has a veth link, `host0`, to which the
host serves DHCP and NAT: it reaches out, but nothing reaches it from
outside the host, and environments do not reach one another. Every shipped
profile configures `host0` (systemd-networkd, or NetworkManager on the
Enterprise Linux ones), so `status` shows an address once it is up.

## Limits

- `/var` holds the images and environments: on the machine's encrypted
  disk if it has one, where an environment is a snapshot of its image and
  takes space only for what it changes; otherwise in RAM, gone at the next
  boot, where it is a copy. A Debian base takes about 240 MB; mind the RAM.
- Building runs the distribution's package manager from its mirrors, so
  the machine needs to reach them.
