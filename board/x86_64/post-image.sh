#!/bin/sh
# Buildroot post-image script: check the kernel configuration, build one UKI
# per slot, assemble disk.img and the signed RAUC bundle.
set -eu

BOARD_DIR=$(dirname "$(readlink -f "$0")")
version=${BR2_EXTERNAL_CHERRY_VERSION#-}
version=${version:-unknown}

linux_dir=$(ls -d "$BUILD_DIR"/linux-[0-9]* | tail -n 1)
"$BOARD_DIR/check-kconfig.sh" "$BOARD_DIR/linux.fragment" "$linux_dir/.config"

# One UKI per slot, differing only in the embedded command line. The UEFI
# entries carry no load options, so systemd-stub always uses this one.
stub="$TARGET_DIR/usr/lib/systemd/boot/efi/linuxx64.efi.stub"
for s in a b; do
	rm -f "$BINARIES_DIR/cherry-$s.efi"
	"$HOST_DIR/bin/python3" "$HOST_DIR/bin/ukify" build \
		--linux="$BINARIES_DIR/bzImage" \
		--stub="$stub" \
		--os-release="@$TARGET_DIR/usr/lib/os-release" \
		--cmdline="root=PARTLABEL=cherry-root-$s rauc.slot=cherry-$s" \
		--output="$BINARIES_DIR/cherry-$s.efi"
done

support/scripts/genimage.sh -c "$BOARD_DIR/genimage.cfg"

bundle_dir="$BUILD_DIR/cherry-bundle"
bundle="$BINARIES_DIR/cherry-x86_64.raucb"
rm -rf "$bundle_dir" "$bundle"
mkdir -p "$bundle_dir"
cp "$BINARIES_DIR/rootfs.squashfs" "$BINARIES_DIR/efi.vfat" "$bundle_dir/"
sed "s/@VERSION@/$version/" "$BOARD_DIR/manifest.raucm.in" > "$bundle_dir/manifest.raucm"
"$HOST_DIR/bin/rauc" bundle \
	--cert="$CHERRY_RAUC_CERT" \
	--key="$CHERRY_RAUC_KEY" \
	--signing-keyring="$CHERRY_RAUC_KEYRING" \
	"$bundle_dir" "$bundle"
"$HOST_DIR/bin/rauc" info --keyring="$CHERRY_RAUC_KEYRING" "$bundle"
