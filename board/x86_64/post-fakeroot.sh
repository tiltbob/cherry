#!/bin/sh
# Runs inside fakeroot on the per-filesystem copy of TARGET_DIR ($1), after
# Buildroot's own rootfs hooks (including the VAR_FACTORY one), so changes here
# only affect the image, not TARGET_DIR.
set -eu

TARGET_DIR=$1
BOARD_DIR=$(dirname "$(readlink -f "$0")")

# systemd-boot and the UKI stub are only needed by post-image.sh.
rm -rf "$TARGET_DIR/usr/lib/systemd/boot"

# Both slots use the same partition types: nothing may be auto-discovered.
rm -f "$TARGET_DIR/usr/lib/systemd/system-generators/systemd-gpt-auto-generator"

# /var is the persistent cherry-data partition, not Buildroot's tmpfs.
install -m 0644 "$BOARD_DIR/var.mount" "$TARGET_DIR/usr/lib/systemd/system/var.mount"
for wants in var.mount.wants local-fs.target.wants; do
	mkdir -p "$TARGET_DIR/usr/lib/systemd/system/$wants"
	ln -sfn ../systemd-growfs@.service \
		"$TARGET_DIR/usr/lib/systemd/system/$wants/systemd-growfs@var.service"
done
