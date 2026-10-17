---
name: Machine images
description: Build and test systemd-nspawn container images that machinectl runs, with the machines tools
---

# Machine images

This machine runs Cherry, a container host: containers are systemd-nspawn
images in `/var/lib/machines`, managed with machinectl. You cannot write
there or run systemd-nspawn yourself; the `machines` tools do it for you:

| Tool | Does |
|---|---|
| `list` | images, running containers, recipes |
| `build` | builds an image from a recipe (minutes) |
| `start` / `stop` | boots / shuts down a container of an image |
| `exec` | runs a shell command in a running container, as root |
| `remove` | removes an image |

## Workflow

1. `build` the image: a distribution, packages, files, a `postinst` script.
2. `start` it, then check it with `exec`: the services run
   (`systemctl is-system-running`, `systemctl status <unit>`), the
   program answers, the files are there.
3. Wrong? Fix the recipe and `build` again with `replace: true` (`stop` it
   first). Change images through the recipe rather than by hand in a running
   container, so the image can be rebuilt as it is.
4. `stop` it when it is done.

Images stay on this machine: there is no tool to write one out for another
machine, which builds its own from a recipe.

## Recipe

`build` takes:

- `distribution` and `release`: `debian` with a codename (`trixie`);
  `fedora` (`44`); `centos-stream`, `almalinux` or `rocky` (`9`, `10`);
  `arch` with no release.
- `packages`: installed with the image's own apt-get, dnf or pacman, on top
  of a base that boots and lets `exec` in: systemd and dbus.
- `files`: `{path, content, mode}` (or `{path, symlink}`), copied in after
  the packages, owned by root. Use the real paths of merged-/usr
  distributions: `/usr/bin`, `/usr/lib`, not `/bin`, `/lib`.
- `postinst`: a shell script run last, in the image, as root, with network:
  `systemctl enable`, `useradd`, configuration that needs the packages.
  Without a `#!` line it runs with `sh` and `set -eu`; any failure fails the
  build.
- `nspawn`: the image's `.nspawn` settings (systemd.nspawn(5)).
  `machinectl start` already gives the container a veth link and a user
  namespace. For images in `/var/lib/machines` only unprivileged settings
  apply, such as `[Exec]` `Environment=`, `Timezone=` and `ResolvConf=`:
  `Bind=`, `Capability=`, the network settings and `Port=` are ignored.

The recipe lands in `/var/lib/cherry/images/<name>/` (`image.conf`,
`extra/`, `postinst`, `image.nspawn`); the build log in
`/var/log/cherry-image/build:<name>.log`. `build` returns its end, and the
whole log is there to read when a build fails.

Every image built has an empty `/etc/machine-id`, so each container gets its
own; no `/etc/hostname` unless the recipe puts one there, so a container is
named after its machine; and no package caches: run `apt-get update` before
`apt-get install` in a container.

## Networking

A container started with `start` has a veth link, `host0`, to which the host
serves DHCP and NAT: it reaches out, but nothing reaches it from outside the
host, and containers do not reach one another. Configure `host0` with
systemd-networkd, whose `80-container-host0.network` asks for DHCP:

- Debian, Arch: networkd is part of systemd; `systemctl enable
  systemd-networkd` in `postinst`.
- Fedora: already installed and enabled.
- CentOS Stream, AlmaLinux, Rocky: no systemd-networkd in their base
  repositories; use NetworkManager.

## Example

```json
{
  "name": "web",
  "distribution": "debian",
  "release": "trixie",
  "packages": ["nginx-light"],
  "files": [{"path": "/var/www/html/index.html", "content": "<h1>hello</h1>\n"}],
  "postinst": "systemctl enable systemd-networkd nginx"
}
```

Then `start` `web` and `exec` `curl -s localhost` (with `curl` among the
packages), or `systemctl is-active nginx`.

## Limits

- `/var` holds the images: on the machine's encrypted disk if it has one,
  otherwise in RAM, gone at the next boot. A Debian base takes about 240 MB;
  mind the RAM.
- Building runs the distribution's package manager from its mirrors, so
  the machine needs to reach them.
