#!/bin/sh
# board/x86_64/post-build.sh still catches a stripped opencode: run against a
# fake TARGET_DIR and BUILD_DIR, laid out as package/opencode's build directory
# is (opencode-<version>/<the binary's path in the .mk>, plus Buildroot's
# .stamp_target_installed). An installed binary identical to the extracted one
# passes; a differing one fails; a missing stamp fails. It also writes
# os-release, moves /root to /var, and drops the 127.0.1.1 line of the
# hostname pattern from /etc/hosts.
set -eu
cd "$(dirname "$0")/.."

version=$(sed -n 's/^OPENCODE_VERSION = //p' package/opencode/opencode.mk)
# shellcheck disable=SC2016 # make's $(...) in the sed expression
binary=$(sed -n 's/.*\$(@D)\/\([^[:space:]]*\)[[:space:]]*\$(TARGET_DIR)\/usr\/bin\/opencode.*/\1/p' package/opencode/opencode.mk)
if [ -z "$version" ] || [ -z "$binary" ]; then
	echo "postbuild: no version or no installed binary in package/opencode/opencode.mk" >&2
	exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export BUILD_DIR="$tmp/build" BR2_EXTERNAL_CHERRY_VERSION=-test
target=$tmp/target
build=$BUILD_DIR/opencode-$version

# A fresh TARGET_DIR and BUILD_DIR, the installed binary identical to the extracted one.
setup() {
	rm -rf "$target" "$BUILD_DIR"
	mkdir -p "$target/usr/bin" "$target/usr/lib" "$target/root" "$target/etc" "$build/$(dirname "$binary")"
	# As Buildroot writes it for BR2_TARGET_GENERIC_HOSTNAME.
	printf '127.0.0.1\tlocalhost\n127.0.1.1\tcherry-????????\n' >"$target/etc/hosts"
	printf 'a Bun single-file executable, say\n' >"$build/$binary"
	cp "$build/$binary" "$target/usr/bin/opencode"
	: >"$build/.stamp_target_installed"
}
run() {
	set +e
	out=$(board/x86_64/post-build.sh "$target" 2>&1)
	rc=$?
	set -e
}
die() {
	echo "postbuild: $*" >&2
	exit 1
}

setup
run
[ "$rc" -eq 0 ] || die "an identical binary failed the check: $out"
grep -qx 'VERSION="test"' "$target/usr/lib/os-release" || die "os-release lacks the version: $(cat "$target/usr/lib/os-release")"
[ "$(readlink "$target/root")" = var/roothome ] || die "/root is not a symlink to var/roothome"
[ "$(cat "$target/etc/hosts")" = "$(printf '127.0.0.1\tlocalhost')" ] ||
	die "/etc/hosts is not just localhost: $(cat "$target/etc/hosts")"
# Buildroot's own name lost its leading dash above; the Makefile's has none.
BR2_EXTERNAL_CHERRY_VERSION=v0.0.1-3-g0123abc-dirty
run
[ "$rc" -eq 0 ] || die "a second run failed: $out"
grep -qx 'VERSION="v0.0.1-3-g0123abc-dirty"' "$target/usr/lib/os-release" ||
	die "os-release lacks the Makefile's version: $(cat "$target/usr/lib/os-release")"

setup
printf 'a bare Bun\n' >"$target/usr/bin/opencode"
run
[ "$rc" -ne 0 ] || die "a differing binary passed the check"
case $out in
*"differs from the extracted binary"*) ;;
*) die "a differing binary failed for another reason: $out" ;;
esac

setup
rm "$build/.stamp_target_installed"
run
[ "$rc" -ne 0 ] || die "a missing stamp passed the check"

echo "postbuild: 3 cases passed (opencode $version, extracted as $binary)"
