#!/bin/sh
# Buildroot post-image script: check the kernel configuration, then wrap the
# kernel, an initramfs holding the squashfs root filesystem, and the kernel
# command line into a single EFI binary (UKI) for UEFI HTTP boot.
set -eu

BOARD_DIR=$(dirname "$(readlink -f "$0")")
linux_dir=$(ls -d "$BUILD_DIR"/linux-[0-9]* | tail -n 1)

"$BOARD_DIR/check-kconfig.sh" "$BOARD_DIR/linux.fragment" "$linux_dir/.config"

# Uncompressed cpio: the squashfs image is already compressed. The kernel's
# gen_init_cpio creates /dev/console without needing root privileges.
cat > "$BUILD_DIR/cherry-initramfs.list" <<EOT
dir /dev 0755 0 0
nod /dev/console 0600 0 0 c 5 1
dir /proc 0755 0 0
dir /sys 0755 0 0
dir /newroot 0755 0 0
file /init $BINARIES_DIR/cherry-init 0755 0 0
file /rootfs.squashfs $BINARIES_DIR/rootfs.squashfs 0444 0 0
EOT
"$linux_dir/usr/gen_init_cpio" "$BUILD_DIR/cherry-initramfs.list" > "$BINARIES_DIR/initramfs.cpio"

efi="$BINARIES_DIR/cherry-x86_64.efi"
rm -f "$efi"
"$HOST_DIR/bin/python3" "$HOST_DIR/bin/ukify" build \
	--linux="$BINARIES_DIR/bzImage" \
	--initrd="$BINARIES_DIR/initramfs.cpio" \
	--stub="$TARGET_DIR/usr/lib/systemd/boot/efi/linuxx64.efi.stub" \
	--os-release="@$TARGET_DIR/usr/lib/os-release" \
	--cmdline="console=tty0 console=ttyS0,115200 panic=10" \
	--output="$efi"
echo "post-image: $(du -h "$efi" | cut -f1) $efi"
