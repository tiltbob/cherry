#!/bin/sh
# package/cherry-secureboot/cherry-secureboot enrolls in setup mode what it
# should, in the right order, and nothing when it shouldn't; out of setup mode
# it reports the state, and sets dbx from the image's dbx.auth on a machine
# with Cherry's PK whose dbx differs, reporting a firmware that refuses it.
# Run against a directory standing in for efivarfs
# (CHERRY_EFIVARS), keys made with scripts/secureboot.py's own structures and
# dummy signatures (the script never checks them, the firmware does), and
# stand-ins on PATH: tpm2_eventlog prints the YAML in $FAKE_EVENTLOG; dd is
# the firmware, which keeps a time-authenticated variable's data without its
# signature, leaves setup mode once the PK is set (unless FAKE_STAY_SETUP), and
# refuses the variable FAKE_FAIL names; chattr and systemctl record their calls.
# Its scratch space is RUNTIME_DIRECTORY, as in the unit, left empty after
# every run; without one it can make, it fails before anything else.
set -eu
cd "$(dirname "$0")/.."
script=$PWD/package/cherry-secureboot/cherry-secureboot

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin" "$tmp/keys" "$tmp/expected" "$tmp/run"
export PATH="$tmp/bin:$PATH" CALLS="$tmp/calls" CHERRY_SECUREBOOT_KEYS="$tmp/keys" \
	CHERRY_EFIVARS="$tmp/efivars" CHERRY_EVENTLOG="$tmp/binary_bios_measurements" FAKE_EVENTLOG="$tmp/eventlog" \
	RUNTIME_DIRECTORY="$tmp/run"
efivars=$CHERRY_EFIVARS
global=8be4df61-93ca-11d2-aa0d-00e098032b8c
security=d719b2cb-3d3a-4596-a3bc-dad00e67656f
marker=CherrySecureBootEnrolled-37310110-2be7-463c-b42a-63e438edf4d7

for tool in chattr systemctl; do
	cat >"$tmp/bin/$tool" <<EOF
#!/bin/sh
echo "$tool \$*" >>"\$CALLS"
EOF
done
cat >"$tmp/bin/tpm2_eventlog" <<'EOF'
#!/bin/sh
[ "${FAKE_FAIL:-}" != tpm2_eventlog ] || { echo "ERROR: failing as told" >&2; exit 1; }
cat "$FAKE_EVENTLOG"
EOF
cat >"$tmp/bin/dd" <<'EOF'
#!/usr/bin/env python3
import os
import struct
import sys

args = dict(a.split("=", 1) for a in sys.argv[1:])
assert args.get("conv") == "notrunc" and args.get("bs") == "1M", args
var = os.path.basename(args["of"])
name = var.split("-", 1)[0]
with open(args["if"], "rb") as f:
    write = f.read()
with open(os.environ["CALLS"], "a") as f:
    f.write(f"dd {var}\n")
if os.environ.get("FAKE_FAIL") == name:
    sys.exit(f"dd: error writing '{args['of']}': Invalid argument")
os.makedirs(os.path.join(os.path.dirname(os.environ["CALLS"]), "writes"), exist_ok=True)
with open(os.path.join(os.path.dirname(os.environ["CALLS"]), "writes", name), "wb") as f:
    f.write(write)
attrs, data = struct.unpack_from("<I", write)[0], write[4:]
if attrs & 0x20:
    data = data[16 + struct.unpack_from("<I", data, 16)[0]:]
with open(args["of"], "wb") as f:
    f.write(struct.pack("<I", attrs) + data)
if name == "PK" and not os.environ.get("FAKE_STAY_SETUP"):
    with open(os.path.join(os.path.dirname(args["of"]), "SetupMode-8be4df61-93ca-11d2-aa0d-00e098032b8c"), "wb") as f:
        f.write(b"\x06\0\0\0\0")
EOF
chmod +x "$tmp/bin/"*

# The keys, the event log, and the variables the firmware should end up with.
python3 - "$tmp" <<'EOF'
import datetime
import hashlib
import os
import struct
import sys

sys.path.insert(0, "scripts")
import secureboot as sb  # noqa: E402

tmp = sys.argv[1]
when = datetime.datetime(2026, 10, 16, 12, 0, 0)
esls = {}
for var in ("PK", "KEK", "db"):
    esls[var] = sb.esl(sb.EFI_CERT_X509, [f"a DER certificate of the {var}".encode()])
    with open(f"{tmp}/keys/{var}.auth", "wb") as f:
        f.write(sb.auth2(sb.efi_time(when), f"a PKCS#7 signature over the {var}".encode(), esls[var]))
# dbx.auth, as revoke writes it: one list per revoked certificate, later than db.auth; kept outside the keys
# until the dbx cases.
esls["dbx"] = b"".join(sb.esl(sb.EFI_CERT_X509, [f"a DER certificate of leaked release key {i}".encode()])
                       for i in (1, 22))
with open(f"{tmp}/dbx.auth", "wb") as f:
    f.write(sb.auth2(sb.efi_time(when + datetime.timedelta(days=1)), b"a PKCS#7 signature over dbx", esls["dbx"]))
drivers = sorted(hashlib.sha256(name).hexdigest() for name in (b"virtio-net", b"gop"))
other = hashlib.sha256(b"cherry-x86_64.efi").hexdigest()


def event(n, pcr, kind, sha256=None):
    digests = f"""  DigestCount: {2 if sha256 else 1}
  Digests:
  - AlgorithmId: sha1
    Digest: "{hashlib.sha1(str(n).encode()).hexdigest()}"
""" + (f"""  - AlgorithmId: sha256
    Digest: "{sha256.upper() if n % 2 else sha256}"
""" if sha256 else "")
    return f"""- EventNum: {n}
  PCRIndex: {pcr}
  EventType: {kind}
{digests}  EventSize: 84
  Event:
    ImageLocationInMemory: 0x7e4b2018
    ImageLengthInMemory: 155648
    ImageLinkTimeAddress: 0x0
    LengthOfDevicePath: 52
    DevicePath: '02010c00d041030a00000000'
"""


head = """---
version: 1
events:
- EventNum: 0
  PCRIndex: 0
  EventType: EV_NO_ACTION
  Digest: "0000000000000000000000000000000000000000"
  EventSize: 37
"""
tail = """pcrs:
  sha256:
    2  : 0x2dd2a7c2fd5b4fcdbc4b8d4b2b5e8e0c6d2a16b8f1b1d6f43b5f8f3a5c3d1e0f
"""
logs = {
    # Two drivers, one of them loaded twice (two cards), a runtime driver, the
    # firmware's own blob in PCR 2 and the boot application in PCR 4.
    "drivers": head + event(1, 0, "EV_S_CRTM_VERSION", other) + event(2, 2, "EV_EFI_BOOT_SERVICES_DRIVER", drivers[0])
    + event(3, 2, "EV_EFI_BOOT_SERVICES_DRIVER", drivers[0]) + event(4, 2, "EV_EFI_RUNTIME_SERVICES_DRIVER", drivers[1])
    + event(5, 2, "EV_EFI_PLATFORM_FIRMWARE_BLOB", other) + event(6, 4, "EV_EFI_BOOT_SERVICES_APPLICATION", other)
    + tail,
    "none": head + event(1, 0, "EV_S_CRTM_VERSION", other) + event(2, 4, "EV_EFI_BOOT_SERVICES_APPLICATION", other)
    + tail,
    "sha1": head + event(1, 2, "EV_EFI_BOOT_SERVICES_DRIVER") + tail,
    "empty": "---\nversion: 1\nevents:\npcrs:\n",
}
for name, text in logs.items():
    with open(f"{tmp}/eventlog.{name}", "w") as f:
        f.write(text)
db = esls["db"] + sb.esl(sb.EFI_CERT_SHA256, [bytes.fromhex(h) for h in drivers])
expected = {"db": db, "db.none": esls["db"], "KEK": esls["KEK"], "PK": esls["PK"], "dbx": esls["dbx"]}
for name, data in expected.items():
    with open(f"{tmp}/expected/{name}", "wb") as f:
        f.write(struct.pack("<I", 0x27) + data)
# What the firmware gets for db with the drivers: db.auth's timestamp and an empty signature.
with open(f"{tmp}/keys/db.auth", "rb") as f:
    auth = f.read()
with open(f"{tmp}/expected/db.write", "wb") as f:
    f.write(struct.pack("<I", 0x27) + auth[:16] + struct.pack("<IHH", 24, 0x0200, 0x0EF1)
            + sb.EFI_CERT_TYPE_PKCS7.bytes_le + db)
EOF

n=0
die() {
	echo "secureboot: case $n: $*" >&2
	exit 1
}
# firmware <SetupMode> <SecureBoot> [<variable> <content file>]...: a fresh
# efivarfs; an empty value leaves the flag unset.
firmware() {
	rm -rf "$efivars" "$tmp/writes"
	mkdir "$efivars"
	# shellcheck disable=SC2059 # the format ends with the flag's byte, in octal
	[ -z "$1" ] || printf "\\006\\000\\000\\000\\$(printf %03o "$1")" >"$efivars/SetupMode-$global"
	# shellcheck disable=SC2059
	[ -z "$2" ] || printf "\\006\\000\\000\\000\\$(printf %03o "$2")" >"$efivars/SecureBoot-$global"
	shift 2
	while [ $# -gt 0 ]; do
		cp "$2" "$efivars/$1"
		shift 2
	done
	cp "$tmp/eventlog.drivers" "$FAKE_EVENTLOG"
	: >"$CHERRY_EVENTLOG"
}
run() {
	: >"$CALLS"
	set +e
	out=$("$script" 2>&1)
	rc=$?
	set -e
	n=$((n + 1))
	[ -z "$(ls -A "$RUNTIME_DIRECTORY" 2>/dev/null)" ] || die "left behind in its runtime directory: $(ls -A "$RUNTIME_DIRECTORY")"
}
calls() {
	[ "$(cat "$CALLS")" = "$1" ] || die "expected the calls:
$1
got:
$(cat "$CALLS")"
}
# says <text>: the output has the text, wherever the banner folded its lines.
says() {
	case $(printf '%s' "$out" | tr -s ' \n' '  ') in
	*"$1"*) ;;
	*) die "expected '$1' in: $out" ;;
	esac
}
same() {
	cmp -s "$1" "$2" || die "$1 differs from $2"
}
# refused <message>: enrollment failed before setting anything.
refused() {
	run
	[ "$rc" -ne 0 ] || die "succeeded: $out"
	says "Cherry did not enroll its Secure Boot keys."
	says "$1"
	calls ""
	[ ! -e "$efivars/PK-$global" ] || die "a PK was set"
}

# Setup mode: db (the CA and the two drivers, unsigned), KEK, PK, in that
# order, then the marker and a reboot.
firmware 1 0
run
[ "$rc" -eq 0 ] || die "failed: $out"
says "Cherry enrolled its Secure Boot keys: rebooting into Secure Boot."
says "and 2 option ROM driver(s)"
calls "dd db-$security
dd KEK-$global
dd PK-$global
dd $marker
systemctl --no-block reboot"
same "$tmp/writes/db" "$tmp/expected/db.write"
for var in "db-$security:db" "KEK-$global:KEK" "PK-$global:PK"; do
	same "$efivars/${var%%:*}" "$tmp/expected/${var#*:}"
done
for var in KEK PK; do
	{ printf "\047\000\000\000"; cat "$tmp/keys/$var.auth"; } | cmp -s - "$tmp/writes/$var" ||
		die "$var was not set to $var.auth as the token signed it"
done
[ "$(od -An -tx1 "$efivars/$marker" | tr -d ' \n')" = 0700000001 ] || die "the marker is not set"

# Setup mode without option ROMs: db.auth as the KEK signed it.
firmware 1 0
cp "$tmp/eventlog.none" "$FAKE_EVENTLOG"
run
[ "$rc" -eq 0 ] || die "failed: $out"
says "and 0 option ROM driver(s)"
{ printf "\047\000\000\000"; cat "$tmp/keys/db.auth"; } | cmp -s - "$tmp/writes/db" ||
	die "db was not set to db.auth as the token signed it"
same "$efivars/db-$security" "$tmp/expected/db.none"

# Setup mode with db and KEK left over (another vendor's): replaced, each
# made writable first.
printf '\047\000\000\000old' >"$tmp/old"
firmware 1 0 "db-$security" "$tmp/old" "KEK-$global" "$tmp/old"
run
[ "$rc" -eq 0 ] || die "failed: $out"
calls "chattr -i $efivars/db-$security
dd db-$security
chattr -i $efivars/KEK-$global
dd KEK-$global
dd PK-$global
dd $marker
systemctl --no-block reboot"
same "$efivars/db-$security" "$tmp/expected/db"
same "$efivars/KEK-$global" "$tmp/expected/KEK"

# Nothing is set when the option ROMs can't be known.
firmware 1 0
cp "$tmp/eventlog.sha1" "$FAKE_EVENTLOG"
refused "turn on the TPM's SHA-256 PCR bank"
firmware 1 0
cp "$tmp/eventlog.empty" "$FAKE_EVENTLOG"
refused "The TPM event log is empty"
firmware 1 0
rm "$CHERRY_EVENTLOG"
refused "There is no TPM event log"
firmware 1 0
export FAKE_FAIL=tpm2_eventlog
refused "tpm2_eventlog cannot read the TPM event log"
unset FAKE_FAIL
firmware 1 0
rm "$tmp/keys/KEK.auth.bak" 2>/dev/null || true
mv "$tmp/keys/KEK.auth" "$tmp/keys/KEK.auth.bak"
refused "KEK.auth is missing"
mv "$tmp/keys/KEK.auth.bak" "$tmp/keys/KEK.auth"

# Setup mode again after an enrollment: the firmware dropped the keys.
firmware 1 0 "$marker" /dev/null
refused "yet the firmware is in setup mode again"

# The firmware refuses the KEK: no PK, no marker, no reboot.
firmware 1 0
export FAKE_FAIL=KEK
run
unset FAKE_FAIL
[ "$rc" -ne 0 ] || die "succeeded: $out"
says "The firmware refused the KEK: dd: error writing"
calls "dd db-$security
dd KEK-$global"

# The firmware takes the PK but stays in setup mode: no marker, no reboot.
firmware 1 0
export FAKE_STAY_SETUP=1
run
unset FAKE_STAY_SETUP
[ "$rc" -ne 0 ] || die "succeeded: $out"
says "but is still in setup mode"
calls "dd db-$security
dd KEK-$global
dd PK-$global"

# Out of setup mode: the state, and the marker cleared; nothing set.
firmware 0 1 "PK-$global" "$tmp/expected/PK" "$marker" /dev/null
run
[ "$rc" -eq 0 ] || die "failed: $out"
says "Secure Boot is on, with Cherry's keys."
calls "chattr -i $efivars/$marker"
[ ! -e "$efivars/$marker" ] || die "the marker was not cleared"
firmware 0 1 "PK-$global" "$tmp/old"
run
says "Secure Boot is on, with another platform key than Cherry's."
calls ""
firmware 0 0 "PK-$global" "$tmp/expected/PK"
run
[ "$rc" -eq 0 ] || die "failed: $out"
says "Cherry's keys are enrolled, but Secure Boot is turned off in the firmware setup"
firmware 0 0 "PK-$global" "$tmp/old"
run
says "put the firmware into setup mode"
firmware "" ""
run
[ "$rc" -eq 0 ] || die "failed: $out"
says "The firmware has no Secure Boot."
calls ""

# dbx.auth in the image. In setup mode, dbx goes in after db, as the KEK
# signed it, and the banner counts its two revoked keys.
cp "$tmp/dbx.auth" "$tmp/keys/dbx.auth"
firmware 1 0
run
[ "$rc" -eq 0 ] || die "failed: $out"
says "and 2 option ROM driver(s) the firmware loaded, and dbx: 2 revoked release key(s)."
calls "dd db-$security
dd dbx-$security
dd KEK-$global
dd PK-$global
dd $marker
systemctl --no-block reboot"
same "$efivars/dbx-$security" "$tmp/expected/dbx"
{ printf "\047\000\000\000"; cat "$tmp/keys/dbx.auth"; } | cmp -s - "$tmp/writes/dbx" ||
	die "dbx was not set to dbx.auth as the token signed it"
# The firmware refuses dbx in setup mode: no KEK, no PK, no reboot.
firmware 1 0
export FAKE_FAIL=dbx
run
unset FAKE_FAIL
[ "$rc" -ne 0 ] || die "succeeded: $out"
says "The firmware refused dbx: dd: error writing"
calls "dd db-$security
dd dbx-$security"
# Out of setup mode, with Cherry's PK: a dbx that isn't dbx.auth's (none, or
# another vendor's, made writable first) becomes it, with Secure Boot on or
# off; one that is already is left alone, as is any dbx under another PK.
firmware 0 1 "PK-$global" "$tmp/expected/PK"
run
[ "$rc" -eq 0 ] || die "failed: $out"
says "Secure Boot is on, with Cherry's keys."
says "dbx is now the image's: 2 revoked release key(s)."
calls "dd dbx-$security"
same "$efivars/dbx-$security" "$tmp/expected/dbx"
firmware 0 0 "PK-$global" "$tmp/expected/PK" "dbx-$security" "$tmp/old"
run
[ "$rc" -eq 0 ] || die "failed: $out"
says "dbx is now the image's"
calls "chattr -i $efivars/dbx-$security
dd dbx-$security"
same "$efivars/dbx-$security" "$tmp/expected/dbx"
firmware 0 1 "PK-$global" "$tmp/expected/PK" "dbx-$security" "$tmp/expected/dbx"
run
[ "$rc" -eq 0 ] || die "failed: $out"
says "Secure Boot is on, with Cherry's keys."
case $out in *dbx*) die "dbx was touched though it is the image's already: $out" ;; esac
calls ""
firmware 0 1 "PK-$global" "$tmp/old" "dbx-$security" "$tmp/old"
run
[ "$rc" -eq 0 ] || die "failed: $out"
says "Secure Boot is on, with another platform key than Cherry's."
calls ""
same "$efivars/dbx-$security" "$tmp/old"
# The firmware refuses it out of setup mode: reported on the console, and the
# unit fails, with Secure Boot's state reported all the same.
firmware 0 1 "PK-$global" "$tmp/expected/PK"
export FAKE_FAIL=dbx
run
unset FAKE_FAIL
[ "$rc" -ne 0 ] || die "succeeded: $out"
says "Secure Boot is on, with Cherry's keys."
says "Cherry could not revoke its leaked release keys."
says "with 2 revoked release key(s), signed by the key exchange key: dd: error writing"
says "Images those keys signed still boot on this machine."
calls "dd dbx-$security"
[ ! -e "$efivars/dbx-$security" ] || die "a refused dbx was set"
rm "$tmp/keys/dbx.auth"

# No scratch space (early in boot, /tmp is read-only): a failure, in setup
# mode as out of it, with nothing set and no reboot.
for mode in 1 0; do
	firmware "$mode" 0 "PK-$global" "$tmp/expected/PK"
	RUNTIME_DIRECTORY=$tmp/none run
	[ "$rc" -ne 0 ] || die "succeeded without scratch space: $out"
	calls ""
	case $out in
	*"Secure Boot is"* | *"enrolled"*) die "reported without scratch space: $out" ;;
	esac
done

echo "secureboot: $n cases passed"
