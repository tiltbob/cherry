#!/bin/sh
# /usr/lib/cherry/var (cherry-var.service) picks the right disk and leaves the
# others alone: run against a stand-in sysfs (CHERRY_SYSFS) and mount table
# (CHERRY_MOUNTINFO) in a temporary directory, with stand-in udevadm, wipefs,
# systemd-repart, systemd-cryptsetup, e2fsck and mount on PATH. wipefs prints
# the signatures in signatures/<disk> (the unlocked volume's in
# signatures/cherry-var), or fails on signatures/<disk>.error.
# The others record their calls; systemd-repart adds the cherry-var partition
# <disk>1 to the sysfs, systemd-cryptsetup, systemd-repart and mount fail when
# FAKE_FAIL names them, and e2fsck exits with FAKE_E2FSCK.
set -eu
cd "$(dirname "$0")/.."
script=$PWD/board/x86_64/rootfs-overlay/usr/lib/cherry/var

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin" "$tmp/signatures"
export PATH="$tmp/bin:$PATH" CHERRY_SYSFS="$tmp/sys" CHERRY_MOUNTINFO="$tmp/mountinfo" CALLS="$tmp/calls" \
	SIGNATURES="$tmp/signatures"
block=$CHERRY_SYSFS/class/block

printf '#!/bin/sh\n' >"$tmp/bin/udevadm"
cat >"$tmp/bin/wipefs" <<'EOF'
#!/bin/sh
for dev; do :; done
dev=${dev#/dev/}
dev=${dev#mapper/}
[ ! -e "$SIGNATURES/$dev.error" ] || { echo "wipefs: $dev: failing as told" >&2; exit 1; }
cat "$SIGNATURES/$dev" 2>/dev/null || true
EOF
cat >"$tmp/bin/systemd-repart" <<'EOF'
#!/bin/sh
echo "systemd-repart $*" >>"$CALLS"
[ "${FAKE_FAIL:-}" != systemd-repart ] || exit 1
for disk; do :; done
disk=${disk#/dev/}
mkdir -p "$CHERRY_SYSFS/class/block/$disk/${disk}1" "$CHERRY_SYSFS/class/block/${disk}1"
: >"$CHERRY_SYSFS/class/block/$disk/${disk}1/partition"
printf 'DEVTYPE=partition\nPARTNAME=cherry-var\n' >"$CHERRY_SYSFS/class/block/${disk}1/uevent"
EOF
cat >"$tmp/bin/systemd-cryptsetup" <<'EOF'
#!/bin/sh
echo "systemd-cryptsetup $*" >>"$CALLS"
[ "${FAKE_FAIL:-}" != systemd-cryptsetup ] || [ "$1" != attach ] || exit 1
EOF
cat >"$tmp/bin/e2fsck" <<'EOF'
#!/bin/sh
echo "e2fsck $*" >>"$CALLS"
exit "${FAKE_E2FSCK:-0}"
EOF
cat >"$tmp/bin/mount" <<'EOF'
#!/bin/sh
echo "mount $*" >>"$CALLS"
[ "${FAKE_FAIL:-}" != mount ] || exit 32
EOF
chmod +x "$tmp/bin/"*

# disk <name> [usb]: a whole disk of 4 GiB on a PCI (or USB) device, writable, not removable, blank.
disk() {
	bus=pci0000:00/virtio0
	[ "${2:-}" != usb ] || bus=pci0000:00/usb1/1-1
	mkdir -p "$CHERRY_SYSFS/devices/$bus/$1" "$block/$1/holders"
	ln -s "$CHERRY_SYSFS/devices/$bus/$1" "$block/$1/device"
	echo 0 >"$block/$1/removable"
	echo 0 >"$block/$1/ro"
	echo 8388608 >"$block/$1/size"
	printf 'DEVTYPE=disk\n' >"$block/$1/uevent"
}
# virtual <name>: a loop, zram or dm device: no device behind it.
virtual() {
	mkdir -p "$block/$1/holders"
	echo 0 >"$block/$1/removable"
	echo 0 >"$block/$1/ro"
	echo 8388608 >"$block/$1/size"
	printf 'DEVTYPE=disk\n' >"$block/$1/uevent"
}
# part <disk> <number> [label]: a partition, with a GPT name.
part() {
	mkdir -p "$block/$1/$1$2" "$block/$1$2"
	: >"$block/$1/$1$2/partition"
	echo "$2" >"$block/$1$2/partition"
	printf 'DEVTYPE=partition\n%s' "${3:+PARTNAME=$3
}" >"$block/$1$2/uevent"
}

# The disks that are never claimed, and loop and zram devices.
unsuitable() {
	virtual loop0
	virtual zram0
	disk nvme0n1
	echo 1 >"$block/nvme0n1/removable"
	disk sda usb
	disk sdb
	echo 1 >"$block/sdb/ro"
	disk sdc
	echo 2097151 >"$block/sdc/size"
	disk sdd
	part sdd 1
	disk sde
	: >"$block/sde/holders/dm-0"
	disk sdf
	echo gpt >"$SIGNATURES/sdf"
	disk sdg
	: >"$SIGNATURES/sdg.error"
}

n=0
die() {
	echo "var: $*" >&2
	exit 1
}
# holds <type>: what the unlocked volume holds.
holds() {
	echo "$1" >"$SIGNATURES/cherry-var"
}
fresh() {
	rm -rf "$CHERRY_SYSFS" "${SIGNATURES:?}"/*
	mkdir -p "$block"
	: >"$CHERRY_MOUNTINFO"
	holds btrfs
}
run() {
	: >"$CALLS"
	set +e
	out=$("$script" 2>&1)
	rc=$?
	set -e
	n=$((n + 1))
}
# expect <case> <exit status> <output> <calls>: the output contains the line.
expect() {
	[ "$rc" -eq "$2" ] || die "$1: exit status $rc, not $2: $out"
	printf '%s\n' "$out" | grep -qxF "$3" || die "$1: no line '$3' in: $out"
	[ "$(cat "$CALLS")" = "$4" ] || die "$1: called:
$(cat "$CALLS")
not:
$4"
}

unlock="systemd-cryptsetup attach cherry-var /dev/vda1 - tpm2-device=auto,headless"
check="e2fsck -p /dev/mapper/cherry-var"
mount="mount -t btrfs -o nodev /dev/mapper/cherry-var /var"
mount_ext4="mount -t ext4 -o nodev /dev/mapper/cherry-var /var"
detach="systemd-cryptsetup detach cherry-var"
repart="systemd-repart --dry-run=no --empty=require --definitions=/usr/lib/cherry/repart.d"
repart="$repart --tpm2-device=auto --tpm2-pcrs= --tpm2-public-key-pcrs= /dev/vda"

fresh
echo '36 25 0:32 / /var rw,nodev,relatime shared:5 - tmpfs tmpfs rw,size=1536000k' >"$CHERRY_MOUNTINFO"
run
expect "/var mounted" 0 "/var is mounted already" ""

fresh
unsuitable
run
expect "no blank disk" 0 "no partition labelled cherry-var and no blank disk: /var is a tmpfs" ""

# The first blank disk, after all those that are not, and only that one.
disk vda
disk vdb
run
expect "first boot" 0 "making the blank disk /dev/vda this machine's encrypted disk for /var" \
	"$repart
$unlock
$mount"
printf '%s\n' "$out" | grep -qxF "/var is on /dev/vda1 (btrfs), unlocked with the TPM" || die "first boot: $out"
[ ! -e "$block/vdb1" ] || die "first boot: vdb was set up too"

# The next boot unlocks the partition it made, and claims nothing.
run
expect "next boot" 0 "/var is on /dev/vda1 (btrfs), unlocked with the TPM" "$unlock
$mount"

# A partition that the TPM cannot unlock: nothing else is touched.
export FAKE_FAIL=systemd-cryptsetup
run
unset FAKE_FAIL
expect "locked" 1 "the TPM cannot unlock /dev/vda1; /var is a tmpfs" "$unlock"
[ ! -e "$block/vdb1" ] || die "locked: vdb was set up"

# An older image's ext4 keeps working, checked first: errors corrected are
# fine, errors left are not.
holds ext4
run
expect "ext4" 0 "/var is on /dev/vda1 (ext4), unlocked with the TPM" "$unlock
$check
$mount_ext4"
export FAKE_E2FSCK=1
run
expect "e2fsck corrected" 0 "/var is on /dev/vda1 (ext4), unlocked with the TPM" "$unlock
$check
$mount_ext4"
export FAKE_E2FSCK=4
run
unset FAKE_E2FSCK
expect "e2fsck failed" 1 "e2fsck found errors it cannot repair on /dev/vda1 (exit status 4); /var is a tmpfs" \
	"$unlock
$check
$detach"

# Anything else is left as it is.
holds xfs
run
expect "xfs" 1 "/dev/vda1 holds xfs, not btrfs or ext4; /var is a tmpfs" "$unlock
$detach"
holds ""
run
expect "unknown" 1 "/dev/vda1 holds nothing wipefs knows, not btrfs or ext4; /var is a tmpfs" "$unlock
$detach"
holds btrfs

export FAKE_FAIL=mount
run
unset FAKE_FAIL
expect "mount failed" 1 "cannot mount /dev/mapper/cherry-var (/dev/vda1) on /var; /var is a tmpfs" "$unlock
$mount
$detach"

# Two cherry-var partitions: neither is used.
part sdd 2 cherry-var
run
expect "two partitions" 1 "more than one partition is labelled cherry-var (sdd2 vda1): using none; /var is a tmpfs" ""

# systemd-repart fails: nothing is unlocked or mounted.
fresh
disk vda
export FAKE_FAIL=systemd-repart
run
unset FAKE_FAIL
expect "repart failed" 1 "systemd-repart could not set up /dev/vda; /var is a tmpfs" "$repart"

echo "var: $n cases passed"
