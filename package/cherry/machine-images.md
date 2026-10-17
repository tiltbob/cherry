---
name: Machine images
description: Create, start, check, stop and remove the container environments of this machine, with the machines tools
---

# Environments

This machine runs Cherry, a container host: environments are systemd-nspawn
images in `/var/lib/machines`, managed with machinectl. You cannot write
there, run systemd-nspawn, or run commands in a container yourself; the
`machines` tools do what can be done:

| Tool | Does |
|---|---|
| `list` | the environments, the containers running, and the profiles |
| `create` | builds an environment from a profile (minutes) |
| `start` / `stop` | boots / shuts down an environment |
| `status` | what a running environment reports: systemd's state, its address, failed units |
| `log` | the end of an environment's build log |
| `remove` | removes an environment |

## Profiles

An environment is created from a profile and a name, nothing else. A profile
is a distribution with its setup, written by a person: the machine ships
some (`/usr/lib/cherry/profiles/`), and its administrator may add more
(`/var/lib/cherry/profiles/`). `list` gives their ids, titles and
descriptions; pick the one whose distribution the request names, and ask
when none fits. There is no way to add packages, files or a script from
here: that takes a new profile, which only a person can write.

Names are up to 48 letters, digits and `-`, starting with a letter or digit.

## Workflow

1. `list`, to see the profiles and whether the name is taken.
2. `create` the environment from the profile. A build takes minutes and
   returns the end of its log; a failed one returns the log too, and `log`
   shows more of it.
3. `start` it, then `status`: systemd `running` and an address on `host0`
   mean it is up. `degraded` names failed units.
4. `stop` it when it is done; `remove` it when it is no longer wanted. An
   environment must be stopped before it is removed or created again
   (`create` with `replace`).

Do not guess: when the request does not say which profile, or names an
environment that `list` does not show, ask.

## Networking

An environment started with `start` has a veth link, `host0`, to which the
host serves DHCP and NAT: it reaches out, but nothing reaches it from
outside the host, and environments do not reach one another. Every shipped
profile configures `host0` (systemd-networkd, or NetworkManager on the
Enterprise Linux ones), so `status` shows an address once it is up.

## Limits

- `/var` holds the environments: on the machine's encrypted disk if it has
  one, otherwise in RAM, gone at the next boot. A Debian base takes about
  240 MB; mind the RAM.
- Building runs the distribution's package manager from its mirrors, so
  the machine needs to reach them.
