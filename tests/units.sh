#!/bin/sh
# The systemd units parse and are wired right: systemd-analyze verify, against
# a fake root that holds what the package .mk files install from their package
# directories ($(INSTALL) -D -m <mode> $(<PKG>_PKGDIR)/<file> $(TARGET_DIR)/<path>)
# and the board's root filesystem overlay. So a unit's ExecStart= must name a
# script where its package installs it, executable; a package's unit must be
# installed by its .mk; and a Requires= must name a unit that exists. systemd's
# own targets and the executables of other packages are stubs. Any message
# from verify is a failure: an unknown key or a bad value is only a warning to
# it. Needs systemd (systemd-analyze).
set -eu
cd "$(dirname "$0")/.."

root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
units=$root/usr/lib/systemd/system

# The overlay, as Buildroot copies it.
cp -a board/x86_64/rootfs-overlay/. "$root"

# The packages' own files, where their .mk files install them.
for mk in package/*/*.mk; do
	pkgdir=$(dirname "$mk")
	# Continuation lines joined, then: mode, file, path.
	# shellcheck disable=SC2016 # make's $(...) in the sed expression
	sed -e :a -e '/\\$/N; s/\\\n//; ta' "$mk" |
		sed -n 's/.*\$(INSTALL) -D -m \([0-7]*\)[[:space:]]*\$([A-Z0-9_]*_PKGDIR)\/\([^[:space:]]*\)[[:space:]]*\$(TARGET_DIR)\(\/[^[:space:]]*\).*/\1 \2 \3/p' |
		while read -r mode file path; do
			install -D -m "$mode" "$pkgdir/$file" "$root$path"
		done
done
for unit in package/*/*.service; do
	if [ ! -e "$units/$(basename "$unit")" ]; then
		echo "units: $unit is not installed by $(dirname "$unit")/*.mk" >&2
		exit 1
	fi
done
# Cherry's units, to verify: a template as an instance.
set --
for unit in "$units"/*.service "$units"/*.target; do
	case $unit in
	*@.service) cp "$unit" "${unit%@.service}@box.service"; set -- "$@" "${unit%@.service}@box.service" ;;
	*) set -- "$@" "$unit" ;;
	esac
done

# systemd's own targets that the units order against, require or want.
for target in basic getty getty-pre multi-user network shutdown swap sysinit time-sync; do
	printf '[Unit]\nDescription=%s (stub)\n' "$target" >"$units/$target.target"
done
# Executables of other packages that ExecStart= lines name.
for exe in bin/sh sbin/mkswap sbin/swapon usr/bin/ipfs usr/bin/mkdir usr/bin/pacman-key usr/bin/ssh-keygen usr/sbin/sshd; do
	install -D -m 0755 /dev/null "$root/$exe"
done
# The units that the overlay's drop-ins (sshd.service.d, var.mount.d) extend,
# verified for the drop-ins' sake.
printf '[Unit]\nDescription=sshd (stub)\n\n[Service]\nExecStart=/usr/sbin/sshd -D\n' >"$units/sshd.service"
printf '[Unit]\nDescription=var (stub)\n\n[Mount]\nWhat=tmpfs\nWhere=/var\nType=tmpfs\n' >"$units/var.mount"
set -- "$@" "$units/sshd.service" "$units/var.mount"

if ! out=$(systemd-analyze verify --man=no --root="$root" "$@" 2>&1) || [ -n "$out" ]; then
	printf '%s\n' "$out" >&2
	echo "units: systemd-analyze verify failed" >&2
	exit 1
fi
echo "units: $# verified:$(for unit; do printf ' %s' "${unit##*/}"; done | sed 's/\.service//g')"
