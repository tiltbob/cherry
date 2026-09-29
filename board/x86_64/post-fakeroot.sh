#!/bin/sh
# Runs inside fakeroot on the per-filesystem copy of TARGET_DIR ($1), so the
# changes only affect the root filesystem image.
set -eu

TARGET_DIR=$1

# systemd-boot and the UKI stub are only needed by post-image.sh.
rm -rf "$TARGET_DIR/usr/lib/systemd/boot"

# Nothing on local disks may be auto-mounted based on partition types.
rm -f "$TARGET_DIR/usr/lib/systemd/system-generators/systemd-gpt-auto-generator"
