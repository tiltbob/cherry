# Shared helpers for Cherry's boot and update scripts (sourced, not executed).
# They run under BusyBox sh/awk.

CHERRY_SLOTS="a b"

die() {
	echo "$*" >&2
	exit 1
}

# Kernel name of the whole disk that holds /, e.g. "vda" or "nvme0n1".
root_disk() {
	basename "$(readlink -f "/sys/dev/block/$(mountpoint -d /)/..")"
}

# GPT partition name of the device holding /, e.g. "cherry-root-a".
root_partname() {
	sed -n 's/^PARTNAME=//p' "/sys/dev/block/$(mountpoint -d /)/uevent"
}

# Kernel name of the partition called $1 on the root disk, e.g. "vda1".
part_by_name() {
	_disk=$(root_disk)
	for _u in /sys/class/block/"$_disk"/"$_disk"*/uevent; do
		if grep -qx "PARTNAME=$1" "$_u" 2>/dev/null; then
			basename "$(dirname "$_u")"
			return 0
		fi
	done
	return 1
}

# Lowercase PARTUUID and partition number of kernel partition $1.
part_uuid() {
	sed -n 's/^PARTUUID=//p' "/sys/class/block/$1/uevent" | tr 'A-F' 'a-f'
}
part_num() {
	cat "/sys/class/block/$1/partition"
}

# Filesystem type mounted at $1 (the topmost mount), from mountinfo.
fstype_of() {
	awk -v mp="$1" '$5 == mp { for (i = 7; i <= NF; i++) if ($i == "-") { t = $(i + 1); break } } END { print t }' /proc/self/mountinfo
}

# The slot named by rauc.slot= on the kernel command line; fails unless there
# is exactly one rauc.slot=cherry-a or rauc.slot=cherry-b.
cmdline_slot() {
	set -- $(tr ' ' '\n' < /proc/cmdline | sed -n 's/^rauc\.slot=cherry-\([ab]\)$/\1/p')
	[ "$#" -eq 1 ] || return 1
	echo "$1"
}

# PARTUUID (lowercase) of the ESP systemd-stub was loaded from, if any.
stub_esp_partuuid() {
	_v=/sys/firmware/efi/efivars/StubDevicePartUUID-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f
	[ -r "$_v" ] || return 0
	tail -c +5 "$_v" | tr -d '\000' | tr 'A-F' 'a-f'
}

# --- UEFI boot entries -------------------------------------------------------
# `efibootmgr -v` prints "Boot####[* ] label<TAB>device-path[optional data]".
# efi_refresh caches one listing; the other helpers parse the cache.

EFI_DUMP=
efi_refresh() {
	EFI_DUMP=$(efibootmgr -v) || die "efibootmgr failed"
}

efi_bootcurrent() {
	printf '%s\n' "$EFI_DUMP" | sed -n 's/^BootCurrent: \([0-9A-Fa-f]\{4\}\)$/\1/p' | tr 'a-f' 'A-F'
}

efi_bootnext() {
	printf '%s\n' "$EFI_DUMP" | sed -n 's/^BootNext: \([0-9A-Fa-f]\{4\}\)$/\1/p' | tr 'a-f' 'A-F'
}

efi_has_bootorder() {
	printf '%s\n' "$EFI_DUMP" | grep -q '^BootOrder:'
}

# Space-separated BootOrder.
efi_bootorder() {
	printf '%s\n' "$EFI_DUMP" | sed -n 's/^BootOrder: //p' | tr ',' ' ' | tr 'a-f' 'A-F'
}

efi_in_bootorder() {
	case " $(efi_bootorder) " in
	*" $1 "*) return 0 ;;
	esac
	return 1
}

# Entry numbers labelled $1, in listing order.
efi_nums() {
	printf '%s\n' "$EFI_DUMP" | awk -v want="$1" '
		/^Boot[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][* ] / {
			label = substr($0, 11)
			tab = index(label, "\t")
			if (tab) label = substr(label, 1, tab - 1)
			if (label == want) print toupper(substr($0, 5, 4))
		}'
}

# Label and device path (plus any optional data) of entry $1.
efi_label() {
	printf '%s\n' "$EFI_DUMP" | awk -v want="$1" '
		toupper(substr($0, 1, 8)) == "BOOT" want {
			label = substr($0, 11)
			tab = index(label, "\t")
			if (tab) label = substr(label, 1, tab - 1)
			print label
			exit
		}'
}
efi_path() {
	printf '%s\n' "$EFI_DUMP" | awk -v want="$1" '
		toupper(substr($0, 1, 8)) == "BOOT" want {
			tab = index($0, "\t")
			if (tab) print substr($0, tab + 1)
			exit
		}'
}

# True if entry $1 boots \EFI\cherry\cherry-$4.efi from partition number $2
# with PARTUUID $3, and carries no load options (they would replace the UKI's
# embedded command line).
efi_entry_ok() {
	_p=$(efi_path "$1" | tr 'A-Z\\' 'a-z/')
	case "$_p" in
	"hd($2,gpt,$3,"*")/file(/efi/cherry/cherry-$4.efi)") return 0 ;;
	esac
	return 1
}
