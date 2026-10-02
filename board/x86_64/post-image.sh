#!/bin/sh
# Buildroot post-image script: check the kernel configuration, then wrap the
# kernel, the initramfs and the kernel command line into a single EFI binary
# (UKI) for UEFI HTTP boot. The initramfs holds the root filesystem as a
# compressed EROFS image, which its /init mounts straight from the file.
set -eu

BOARD_DIR=$(dirname "$(readlink -f "$0")")
linux_dir=$(ls -d "$BUILD_DIR"/linux-[0-9]* | tail -n 1)

"$BOARD_DIR/check-kconfig.sh" "$BOARD_DIR/linux.fragment" "$linux_dir/.config"

# The initramfs: stage 1 (package/cherry-stage1) as /init, and Buildroot's
# EROFS image of the root filesystem. That image is compressed already, so the
# archive isn't.
initramfs="$BINARIES_DIR/initramfs.cpio"
"$linux_dir/usr/gen_init_cpio" -t 0 -o "$initramfs" - <<EOF
dir /dev 0755 0 0
nod /dev/console 0600 0 0 c 5 1
dir /sysroot 0755 0 0
file /init $BINARIES_DIR/stage1 0755 0 0
file /rootfs.erofs $BINARIES_DIR/rootfs.erofs 0400 0 0
EOF

# rootfstype=ramfs: unpack the initramfs into a ramfs rather than a tmpfs.
# EROFS mounts files only from filesystems that can read_folio(), and tmpfs
# can't. ramfs pages can't be swapped either, which suits an image that is
# compressed already.
efi="$BINARIES_DIR/cherry-x86_64.efi"
rm -f "$efi"
"$HOST_DIR/bin/python3" "$HOST_DIR/bin/ukify" build \
	--linux="$BINARIES_DIR/bzImage" \
	--initrd="$initramfs" \
	--stub="$TARGET_DIR/usr/lib/systemd/boot/efi/linuxx64.efi.stub" \
	--os-release="@$TARGET_DIR/usr/lib/os-release" \
	--cmdline="console=tty0 console=ttyS0,115200 panic=10 rootfstype=ramfs" \
	--output="$efi"
echo "post-image: $(du -h "$efi" | cut -f1) $efi"
