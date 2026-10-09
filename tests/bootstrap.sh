#!/bin/sh
# package/cherry/cherry-bootstrap checks its instance and runs the right tool
# with the right arguments: run with stand-in debootstrap, rpmstrap, pacstrap
# and systemctl on PATH, LOGS_DIRECTORY in a temporary directory, and the
# machines directory redirected there (CHERRY_MACHINES_DIR). Each stand-in
# prints its name and arguments (into the bootstrap log), records the call,
# creates the tree it is given, and fails when FAKE_FAIL names it.
set -eu
cd "$(dirname "$0")/.."
script=$PWD/package/cherry/cherry-bootstrap

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin" "$tmp/logs" "$tmp/machines"
export PATH="$tmp/bin:$PATH" LOGS_DIRECTORY="$tmp/logs" CHERRY_MACHINES_DIR="$tmp/machines" CALLS="$tmp/calls"
machines=$CHERRY_MACHINES_DIR

for tool in debootstrap rpmstrap pacstrap systemctl; do
	cat >"$tmp/bin/$tool" <<EOF
#!/bin/sh
echo "$tool \$*"
echo "$tool \$*" >>"\$CALLS"
for arg; do
	case \$arg in "\$CHERRY_MACHINES_DIR"/*) mkdir -p "\$arg" ;; esac
done
[ "\${FAKE_FAIL:-}" != $tool ] || { echo "$tool: failing as told" >&2; exit 1; }
EOF
	chmod +x "$tmp/bin/$tool"
done
# After pacstrap, the script clears /var/cache/pacman/pkg: not on this host.
cat >"$tmp/bin/rm" <<'EOF'
#!/bin/sh
for arg; do
	case $arg in /var/cache/pacman/*) echo "rm $*" >>"$CALLS"; exit 0 ;; esac
done
exec /bin/rm "$@"
EOF
chmod +x "$tmp/bin/rm"

n=0
die() {
	echo "bootstrap: $*" >&2
	exit 1
}
run() {
	: >"$CALLS"
	set +e
	out=$("$script" "$1" 2>&1)
	rc=$?
	set -e
	n=$((n + 1))
}
# refused <instance> <message>: exits non-zero with the message, calls no tool, creates no tree.
refused() {
	run "$1"
	[ "$rc" -ne 0 ] || die "'$1' was accepted"
	case $out in
	*"$2"*) ;;
	*) die "'$1': expected '$2', got: $out" ;;
	esac
	[ ! -s "$CALLS" ] || die "'$1': a tool was called: $(cat "$CALLS")"
	[ -z "$(ls -A "$machines")" ] || die "'$1': a tree was created: $(ls -A "$machines")"
}

refused 'debian:trixie:' "bad machine name ''"
refused 'debian:trixie:.hidden' "bad machine name '.hidden'"
refused 'debian:trixie:a/b' "bad machine name 'a/b'"
refused 'debian:trixie:a b' "bad machine name 'a b'"
refused 'debian:-x:box' "bad distribution or release '-x'"
refused '-debian:trixie:box' "bad distribution or release '-debian'"
refused 'arch:rolling:box' "Arch Linux has no release"

run 'debian:trixie:box'
log=$LOGS_DIRECTORY/debian:trixie:box.log
[ "$rc" -eq 0 ] || die "debian:trixie:box failed: $out"
[ "$out" = "bootstrapping $machines/box, log in $log" ] || die "unexpected output: $out"
[ -d "$machines/box" ] || die "debian:trixie:box left no tree"
grep -qxF "debootstrap --variant=minbase --include=systemd,systemd-sysv,dbus trixie $machines/box" "$log" ||
	die "the log lacks debootstrap's line: $(cat "$log")"
grep -qxF "done: machinectl start box" "$log" || die "the log lacks the done line: $(cat "$log")"
[ "$(wc -l <"$CALLS")" -eq 1 ] || die "more than debootstrap was called: $(cat "$CALLS")"

touch "$machines/box/keep"
run 'debian:trixie:box'
[ "$rc" -ne 0 ] || die "the same name again was accepted"
case $out in
*"$machines/box exists"*) ;;
*) die "the same name again: unexpected output: $out" ;;
esac
[ ! -s "$CALLS" ] || die "the same name again called a tool: $(cat "$CALLS")"
[ -e "$machines/box/keep" ] || die "the same name again touched the tree"

export FAKE_FAIL=debootstrap
run 'debian:trixie:gone'
unset FAKE_FAIL
[ "$rc" -ne 0 ] || die "a failing debootstrap was reported as success"
[ ! -e "$machines/gone" ] || die "a failing debootstrap left its tree behind"
grep -q "failing as told" "$LOGS_DIRECTORY/debian:trixie:gone.log" || die "the failing tool's output is not in the log"

run 'fedora:44:f1'
[ "$rc" -eq 0 ] || die "fedora:44:f1 failed: $out"
[ "$(cat "$CALLS")" = "rpmstrap fedora 44 $machines/f1" ] || die "fedora:44:f1 called: $(cat "$CALLS")"
[ -d "$machines/f1" ] || die "fedora:44:f1 left no tree"

run 'arch::a1'
[ "$rc" -eq 0 ] || die "arch::a1 failed: $out"
expected="systemctl start pacman-init.service
pacstrap -K -c $machines/a1 base"
[ "$(head -n 2 "$CALLS")" = "$expected" ] || die "arch::a1 called: $(cat "$CALLS")"
case $(sed -n 3p "$CALLS") in
"rm -rf /var/cache/pacman/pkg/"*) ;;
*) die "arch::a1 did not clear the package cache: $(cat "$CALLS")" ;;
esac
[ "$(wc -l <"$CALLS")" -eq 3 ] || die "arch::a1 called more: $(cat "$CALLS")"
[ -d "$machines/a1" ] || die "arch::a1 left no tree"

echo "bootstrap: $n cases passed"
