#!/bin/sh
# Buildroot post-image script: check the kernel configuration, then wrap the
# kernel, the root filesystem (Buildroot's cpio, used as the initramfs; systemd
# runs from it as PID 1) and the kernel command line into a single EFI binary
# (UKI) for UEFI HTTP boot.
set -eu

BOARD_DIR=$(dirname "$(readlink -f "$0")")
linux_dir=$(ls -d "$BUILD_DIR"/linux-[0-9]* | tail -n 1)

"$BOARD_DIR/check-kconfig.sh" "$BOARD_DIR/linux.fragment" "$linux_dir/.config"

efi="$BINARIES_DIR/cherry-x86_64.efi"
rm -f "$efi"
"$HOST_DIR/bin/python3" "$HOST_DIR/bin/ukify" build \
	--linux="$BINARIES_DIR/bzImage" \
	--initrd="$BINARIES_DIR/rootfs.cpio.zst" \
	--stub="$TARGET_DIR/usr/lib/systemd/boot/efi/linuxx64.efi.stub" \
	--os-release="@$TARGET_DIR/usr/lib/os-release" \
	--cmdline="console=tty0 console=ttyS0,115200 panic=10" \
	--output="$efi"
echo "post-image: $(du -h "$efi" | cut -f1) $efi"
