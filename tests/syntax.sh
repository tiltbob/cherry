#!/bin/sh
# Every shell script under package/ and board/ parses, in the shell its #!
# line names (sh -n; bash -n for rpmstrap), and the shell scripts of the
# checks themselves pass shellcheck. Buildroot's check-package runs shellcheck
# over the scripts under package/ (make check-package); nothing lints board/
# beyond this.
set -eu
cd "$(dirname "$0")/.."

scripts=$(mktemp)
trap 'rm -f "$scripts"' EXIT
find package board -type f -exec awk 'FNR == 1 && /^#!/ { print FILENAME }' {} + | sort >"$scripts"
n=0
while read -r f; do
	case $(head -n 1 "$f") in
	'#!/bin/sh') sh -n "$f" ;;
	'#!/bin/bash') bash -n "$f" ;;
	*) echo "$f: unexpected interpreter: $(head -n 1 "$f")" >&2; exit 1 ;;
	esac
	n=$((n + 1))
done <"$scripts"
shellcheck tests/*.sh
echo "syntax: $n scripts under package/ and board/ parse, tests/*.sh pass shellcheck"
