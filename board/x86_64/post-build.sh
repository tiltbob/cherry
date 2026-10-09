#!/bin/sh
# Buildroot post-build script, run on TARGET_DIR ($1) after every `make`:
# every step must be idempotent.
set -eu

TARGET_DIR=$1
version=${BR2_EXTERNAL_CHERRY_VERSION#-}
version=${version:-unknown}

cat > "$TARGET_DIR/usr/lib/os-release" <<EOT
NAME=Cherry
ID=cherry
PRETTY_NAME="Cherry $version"
VERSION="$version"
IMAGE_ID=cherry
IMAGE_VERSION=$version
HOME_URL="https://github.com/tiltbob/cherry"
EOT

# The root filesystem is read-only: root's home lives on /var (a tmpfs for
# now), so e.g. systemd's ssh.authorized_keys.root credential can be applied.
# The target must exist at build time for makedevs; Buildroot's VAR_FACTORY
# turns it into a factory default copied into /var at boot.
mkdir -p "$TARGET_DIR/var/roothome"
chmod 0700 "$TARGET_DIR/var/roothome"
if [ ! -L "$TARGET_DIR/root" ]; then
	cp -a "$TARGET_DIR/root/." "$TARGET_DIR/var/roothome/" 2>/dev/null || true
	rm -rf "$TARGET_DIR/root"
	ln -s var/roothome "$TARGET_DIR/root"
fi

# opencode is a Bun single-file executable, and stripping it breaks it: it then
# runs as a bare Bun that no longer finds the program embedded in it (see
# package/opencode). The defconfig's BR2_STRIP_EXCLUDE_FILES keeps strip off
# it; make sure the installed binary is still the one that was extracted (the
# most recently installed build directory, should several versions remain).
if [ -e "$TARGET_DIR/usr/bin/opencode" ]; then
	stamp=$(ls -t "$BUILD_DIR"/opencode-*/.stamp_target_installed 2>/dev/null | head -n 1)
	if [ -z "$stamp" ] || ! cmp -s "${stamp%/*}/bin/opencode" "$TARGET_DIR/usr/bin/opencode"; then
		echo "post-build: /usr/bin/opencode differs from the extracted binary (stripped?):" \
			"add opencode to BR2_STRIP_EXCLUDE_FILES" >&2
		exit 1
	fi
fi
