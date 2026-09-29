#!/bin/sh
# Verify that every option requested in linux.fragment made it into the final
# kernel .config. merge_config and Buildroot's kconfig fixups drop symbols whose
# dependencies are unmet without complaining, so check explicitly.
set -eu

fragment=$1
config=$2

missing=0
while IFS= read -r line; do
	case "$line" in
	"# CONFIG_"*" is not set")
		sym=${line#\# }
		sym=${sym%% *}
		# Invisible symbols are not written at all, so only reject y/m.
		if grep -Eq "^${sym}=(y|m)\$" "$config"; then
			echo "check-kconfig: $sym is enabled but must be off" >&2
			missing=1
		fi
		;;
	CONFIG_*)
		if ! grep -qxF "$line" "$config"; then
			echo "check-kconfig: missing '$line' (got: $(grep -E "^(# )?${line%%=*}[= ]" "$config" || echo unset))" >&2
			missing=1
		fi
		;;
	esac
done < "$fragment"

if [ "$missing" -ne 0 ]; then
	echo "check-kconfig: $config does not satisfy $fragment" >&2
	exit 1
fi
echo "check-kconfig: all $(grep -c '^CONFIG_\|^# CONFIG_' "$fragment") fragment options present"
