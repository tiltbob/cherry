#!/bin/sh
# Buildroot post-image script: check the kernel configuration, then wrap the
# kernel, the initramfs and the kernel command line into a single EFI binary
# (UKI) for UEFI HTTP boot, with the signed PCR 11 policy that binds the
# disks' keys to it, and signed for Secure Boot when the build has a local
# chain. The initramfs holds the root filesystem as a compressed EROFS image,
# which its /init mounts straight from the file.
set -eu

BOARD_DIR=$(dirname "$(readlink -f "$0")")
tree=$(readlink -f "$BOARD_DIR/../..")
linux_dir=$(ls -d "$BUILD_DIR"/linux-[0-9]* | tail -n 1)

"$BOARD_DIR/check-kconfig.sh" "$BOARD_DIR/linux.fragment" "$linux_dir/.config"

# The initramfs: cherry-pit (package/cherry-pit) as /init, and Buildroot's
# EROFS image of the root filesystem. That image is compressed already, so the
# archive isn't.
initramfs="$BINARIES_DIR/initramfs.cpio"
"$linux_dir/usr/gen_init_cpio" -t 0 -o "$initramfs" - <<EOF
dir /dev 0755 0 0
nod /dev/console 0600 0 0 c 5 1
dir /sysroot 0755 0 0
file /init $BINARIES_DIR/cherry-pit 0755 0 0
file /rootfs.erofs $BINARIES_DIR/rootfs.erofs 0400 0 0
EOF

# The PCR key signs the policy that binds the disks' keys to the image: the
# values systemd-stub measures the UKI's sections into PCR 11 to, and what
# systemd extends them to as the boot goes on. ukify puts the key's public half
# in the UKI's .pcrpkey section and the signatures in .pcrsig, with
# scripts/pcrsign.py standing in for systemd-measure, which Buildroot's host
# systemd doesn't build (it needs the TPM library). A build tree signs with a
# key of its own, pcr.key next to its images, made at the first build and kept
# after: a rebuilt image still unlocks the disks a VM sealed to the last one,
# until `make clean`, which takes the key with it.
key=$BASE_DIR/pcr.key
public=$BASE_DIR/pcr.pem
if [ ! -s "$key" ]; then
	echo "post-image: making this build tree's PCR key, $key"
	(umask 077 && openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$key")
fi
openssl pkey -in "$key" -pubout -out "$public"
tools=$(mktemp -d)
trap 'rm -rf "$tools"' EXIT
ln -s "$tree/scripts/pcrsign.py" "$tools/systemd-measure"

# rootfstype=ramfs: unpack the initramfs into a ramfs rather than a tmpfs.
# EROFS mounts files only from filesystems that can read_folio(), and tmpfs
# can't. ramfs pages can't be swapped either, which suits an image that is
# compressed already.
uki() {
	"$HOST_DIR/bin/python3" "$HOST_DIR/bin/ukify" build \
		--linux="$BINARIES_DIR/bzImage" \
		--initrd="$initramfs" \
		--stub="$TARGET_DIR/usr/lib/systemd/boot/efi/linuxx64.efi.stub" \
		--os-release="@$TARGET_DIR/usr/lib/os-release" \
		--cmdline="console=tty0 console=ttyS0,115200 panic=10 rootfstype=ramfs" \
		--pcr-banks=sha256 --tools="$tools" "$@"
}
efi="$BINARIES_DIR/cherry-x86_64.efi"
rm -f "$efi"
uki --pcr-private-key="$key" --pcr-public-key="$public" --output="$efi"
"$HOST_DIR/bin/python3" "$tree/scripts/pcrsign.py" check "$efi"
echo "post-image: $(du -h "$efi" | cut -f1) $efi, its PCR policy signed with $key"

# A local chain (README, Secure Boot): the image carries the Secure Boot keys
# of BR2_PACKAGE_CHERRY_SECUREBOOT_KEYS (package/cherry-secureboot), the
# repository's by default. When that directory also holds release.key beside
# release.crt, as scripts/secureboot.py issue --key-out writes it for a chain
# of the developer's own, the image is signed with it, and a machine or VM
# that enrolled that chain boots the local build with Secure Boot on. The
# repository's directory never holds the key: releases are signed in CI, with
# the release environment's secret (.github/workflows/build.yml).
keys=$(sed -n 's/^BR2_PACKAGE_CHERRY_SECUREBOOT_KEYS="\(.*\)"$/\1/p' "$BR2_CONFIG")
# shellcheck disable=SC2016 # the literal make reference the default value holds
case $keys in
'$(BR2_EXTERNAL_CHERRY_PATH)'*) keys=$tree${keys#'$(BR2_EXTERNAL_CHERRY_PATH)'} ;;
esac
if [ -n "$keys" ] && [ -s "$keys/release.key" ]; then
	for tool in sbsign sbverify; do
		if ! command -v "$tool" >/dev/null; then
			echo "post-image: $keys/release.key is there to sign the image with, but $tool is not installed (sbsigntool)" >&2
			exit 1
		fi
	done
	sbsign --key "$keys/release.key" --cert "$keys/release.crt" --output "$efi.signed" "$efi"
	sbverify --cert "$keys/release.crt" "$efi.signed"
	mv "$efi.signed" "$efi"
	echo "post-image: $efi signed for Secure Boot with $keys/release.key, the local chain's"
fi

# The release image: the same, with the release PCR key
# (board/x86_64/secureboot/pcr.pem, from scripts/secureboot.py pcr) as its
# .pcrpkey, and its policy digests in .pcrsig, unsigned, with room for the
# signatures: the release job of .github/workflows/build.yml signs them with
# the key's private half, which only it has. Until the repository has that
# key, a release carries the build's key, and unlocks only the disks it sealed
# itself.
release_key=$BOARD_DIR/secureboot/pcr.pem
release="$BINARIES_DIR/cherry-x86_64-release.efi"
rm -f "$release"
if [ -s "$release_key" ]; then
	uki --pcr-public-key="$release_key" --policy-digest --output="$release" >/dev/null
	"$HOST_DIR/bin/python3" "$tree/scripts/pcrsign.py" check --unsigned "$release"
	echo "post-image: $(du -h "$release" | cut -f1) $release, its PCR policy to be signed with the release key"
fi
