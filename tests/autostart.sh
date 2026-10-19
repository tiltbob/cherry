#!/bin/sh
# package/cherry/cherry-autostart, cherry-autostart.service's script, starts
# the environments whose record says Enabled=yes, all in one systemctl start,
# and nothing else: not one disabled or with no Enabled= line, not a record
# whose name is no machine name, and nothing when there is none; one that
# fails to start is reported, and the unit still succeeds, as machines.target
# would with a wanted unit that failed. Run with a stand-in systemctl on PATH,
# which records its calls and fails when FAKE_FAIL names one of its units,
# and the records in a temporary directory (CHERRY_ENVIRONMENTS_DIR). Needs
# sh and grep.
set -eu
cd "$(dirname "$0")/.."
script=$PWD/package/cherry/cherry-autostart

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin" "$tmp/environments"
export PATH="$tmp/bin:$PATH" CHERRY_ENVIRONMENTS_DIR="$tmp/environments" CALLS="$tmp/calls" LC_ALL=C
cat >"$tmp/bin/systemctl" <<'STANDIN'
#!/bin/sh
echo "systemctl $*" >>"$CALLS"
rc=0
for arg; do
	[ "$arg" != "${FAKE_FAIL:-}" ] || { echo "Job for $arg failed." >&2; rc=1; }
done
exit $rc
STANDIN
chmod +x "$tmp/bin/systemctl"

n=0
die() {
	echo "autostart: $*" >&2
	exit 1
}
# record <name> [line...]: a record as environments.js writes it, plus the lines given.
record() {
	dir=$CHERRY_ENVIRONMENTS_DIR/$1
	shift
	mkdir -p "$dir"
	{
		printf 'Image=web\nCreated=2026-01-01T00:00:00.000Z\n'
		for line; do printf '%s\n' "$line"; done
	} >"$dir/environment.conf"
}
run() {
	: >"$CALLS"
	set +e
	out=$("$script" 2>"$tmp/err")
	rc=$?
	set -e
	err=$(cat "$tmp/err")
	n=$((n + 1))
}
# none <why>: nothing was started, and the script said so.
none() {
	run
	[ "$rc" -eq 0 ] || die "$1: exited with $rc: $out $err"
	[ "$out" = "no environment is enabled" ] || die "$1: unexpected output: $out"
	[ ! -s "$CALLS" ] || die "$1: systemctl was called: $(cat "$CALLS")"
}

# No records directory at all (/var in RAM, and the agent never ran), then none, then none enabled.
rmdir "$CHERRY_ENVIRONMENTS_DIR"
none "without the directory"
mkdir "$CHERRY_ENVIRONMENTS_DIR"
none "without records"
record off Enabled=no
record never
mkdir "$CHERRY_ENVIRONMENTS_DIR/empty"
none "with none enabled"
[ -z "$err" ] || die "with none enabled: complained: $err"

# The enabled ones, in one start, waited for; names that are no machine name are reported and skipped.
record yarp Enabled=yes
record 'd-1.x_2' Enabled=yes
record 'bad name' Enabled=yes
record -bad Enabled=yes
record .hidden Enabled=yes
long=$(printf '%065d' 0)
record "$long" Enabled=yes
run
[ "$rc" -eq 0 ] || die "exited with $rc: $out $err"
expected="systemctl start --no-ask-password systemd-nspawn@d-1.x_2.service systemd-nspawn@yarp.service"
[ "$(cat "$CALLS")" = "$expected" ] || die "called: $(cat "$CALLS")"
[ "$out" = "starting: systemd-nspawn@d-1.x_2.service systemd-nspawn@yarp.service" ] || die "unexpected output: $out"
expected="not a machine name, not started: '-bad'
not a machine name, not started: '$long'
not a machine name, not started: 'bad name'"
[ "$err" = "$expected" ] || die "unexpected complaints: $err"

# A record's Enabled=yes is exact: not a comment, not a prefix, not another value.
rm -rf "${CHERRY_ENVIRONMENTS_DIR:?}"/*
record a '# Enabled=yes'
record b 'Enabled=yes please'
record c 'Enabled=Yes'
record d 'Enabled=yes'
run
[ "$rc" -eq 0 ] || die "exact: exited with $rc: $out $err"
[ "$(cat "$CALLS")" = "systemctl start --no-ask-password systemd-nspawn@d.service" ] || die "exact: called: $(cat "$CALLS")"

# One that fails to start is reported, and the rest were started in the same call; the script still succeeds.
rm -rf "${CHERRY_ENVIRONMENTS_DIR:?}"/*
record a Enabled=yes
record b Enabled=yes
export FAKE_FAIL=systemd-nspawn@a.service
run
unset FAKE_FAIL
[ "$rc" -eq 0 ] || die "a failing start failed the script: $out $err"
[ "$(cat "$CALLS")" = "systemctl start --no-ask-password systemd-nspawn@a.service systemd-nspawn@b.service" ] ||
	die "with a failure, called: $(cat "$CALLS")"
expected="Job for systemd-nspawn@a.service failed.
not every environment started: see systemctl --failed"
[ "$err" = "$expected" ] || die "with a failure, unexpected complaints: $err"

echo "autostart: $n cases passed"
