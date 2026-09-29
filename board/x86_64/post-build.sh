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

keyring=${CHERRY_RAUC_KEYRING:?not set, build through the top-level Makefile}
[ -r "$keyring" ] || { echo "post-build: RAUC keyring $keyring not found (run 'make keys')" >&2; exit 1; }
install -D -m 0644 "$keyring" "$TARGET_DIR/usr/lib/rauc/keyring.pem"

# /root and /home live on /var. The targets must exist at build time: makedevs
# chowns /root, and VAR_FACTORY turns them into factory defaults for /var.
mkdir -p "$TARGET_DIR/var/roothome" "$TARGET_DIR/var/home"
chmod 0700 "$TARGET_DIR/var/roothome"
if [ ! -L "$TARGET_DIR/root" ]; then
	cp -a "$TARGET_DIR/root/." "$TARGET_DIR/var/roothome/" 2>/dev/null || true
	rm -rf "$TARGET_DIR/root"
	ln -s var/roothome "$TARGET_DIR/root"
fi
if [ ! -L "$TARGET_DIR/home" ]; then
	rm -rf "$TARGET_DIR/home"
	ln -s var/home "$TARGET_DIR/home"
fi

# A broken pre-init panics every boot of the slot; catch the obvious mistakes.
for f in "$TARGET_DIR"/usr/lib/cherry/*; do
	case "$f" in
	*.sh) ;;
	*) [ -x "$f" ] || { echo "post-build: $f is not executable" >&2; exit 1; } ;;
	esac
	sh -n "$f"
done
