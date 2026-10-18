#!/bin/sh
# scripts/secureboot.py makes Cherry's root of trust on a token and checks it:
# run against a SoftHSM token standing in for the Nitrokey, all in a temporary
# directory. An init stopped after a key (by a pkcs11-tool that cannot sign)
# removes its files and prints the commands that delete what it generated, and
# the next init refuses to run over that until they ran. issue refuses a wrong
# PIN, a token without the CA's key, and one whose key of that name is not the
# CA's, before it hands out a key. init writes the six files and check verifies
# them; init refuses to run again, over the files or over the keys on the
# token; issue writes release.crt and its key, to a new file or stdout, also
# with its scratch space on another file system (/dev/shm, when it is one).
# revoke adds a release certificate to dbx.auth, signed by the KEK, keeps the
# ones revoked before, refuses one revoked already and one the CA didn't
# issue, and check then wants a new release key, which issue gives. check
# catches a tampered file, a swapped one, a release certificate the CA didn't
# issue and a tampered dbx.auth. pcr, which needs no token, writes pcr.pem and
# the key, once:
# a second pcr refuses, as the key's loss locks every disk; check takes
# pcr.pem with the Secure Boot keys or alone, and catches one that is no
# RSA-2048 key. The token's part is skipped without softhsm2-util and
# pkcs11-tool (OpenSC).
set -eu
cd "$(dirname "$0")/.."

n=0
die() {
	echo "ceremony: case $n: $*" >&2
	exit 1
}
# sb <command> [<argument>...]: scripts/secureboot.py on the token, into $keys.
sb() {
	set +e
	out=$(python3 scripts/secureboot.py --dir "$keys" "$@" 2>&1)
	rc=$?
	set -e
	n=$((n + 1))
}
ok() {
	[ "$rc" -eq 0 ] || die "failed: $out"
}
refused() {
	[ "$rc" -ne 0 ] || die "succeeded: $out"
	case $out in
	*"$1"*) ;;
	*) die "expected '$1' in: $out" ;;
	esac
}

# The PCR key, with openssl alone.
pcr=$(mktemp -d)
trap 'rm -rf "$pcr"' EXIT
keys=$pcr/keys
sb pcr --key-out "$pcr/pcr.key"
ok
[ "$(stat -c %a "$pcr/pcr.key")" = 600 ] || die "the PCR key is not private to its owner"
[ "$(openssl pkey -in "$pcr/pcr.key" -pubout)" = "$(cat "$keys/pcr.pem")" ] || die "pcr.pem is not the key's public half"
openssl pkey -pubin -in "$keys/pcr.pem" -noout -text | grep -q "Public-Key: (2048 bit)" || die "pcr.pem is not RSA-2048"
case $out in *"PCR_KEY"*) ;; *) die "pcr did not say what to do with the key: $out" ;; esac
sb check
ok
case $out in *"no Secure Boot keys"*"pcr.pem is the PCR key"*) ;; *) die "check without Secure Boot keys: $out" ;; esac
sb pcr --key-out "$pcr/other.key"
refused "pcr.pem exists"
[ ! -e "$pcr/other.key" ] || die "a refused pcr wrote a key"
set +e
python3 scripts/secureboot.py --dir "$pcr/stdout" pcr --key-out - >"$pcr/stdout.key" 2>/dev/null
rc=$?
set -e
n=$((n + 1))
ok
openssl pkey -in "$pcr/stdout.key" -noout || die "pcr --key-out - printed no key"
openssl ecparam -name prime256v1 -genkey -noout 2>/dev/null | openssl pkey -pubout >"$keys/pcr.pem"
sb check
refused "pcr.pem: not an RSA-2048 public key"
rm -rf "$keys"

module=
for m in /usr/lib/softhsm/libsofthsm2.so /usr/lib64/pkcs11/libsofthsm2.so /usr/lib/x86_64-linux-gnu/softhsm/libsofthsm2.so; do
	[ -e "$m" ] && module=$m && break
done
if [ -z "$module" ] || ! command -v softhsm2-util >/dev/null || ! command -v pkcs11-tool >/dev/null; then
	echo "ceremony: $n cases passed; the token's SKIPPED, needs softhsm2 and opensc (scripts/secureboot.py init and issue unchecked)"
	exit 0
fi

tmp=$(mktemp -d)
# A temporary directory on another file system than the keys, as a tmpfs /tmp is
# (Arch's): /dev/shm, when it is one, for the scratch space of an issue.
other=
if [ -d /dev/shm ] && [ -w /dev/shm ] && [ "$(stat -c %d /dev/shm)" != "$(stat -c %d "$tmp")" ]; then
	other=$(mktemp -d /dev/shm/ceremony.XXXXXX)
fi
trap 'rm -rf "$tmp" "$pcr" ${other:+"$other"}' EXIT
mkdir "$tmp/tokens"
printf 'directories.tokendir = %s\nobjectstore.backend = file\nlog.level = ERROR\n' "$tmp/tokens" >"$tmp/softhsm2.conf"
export SOFTHSM2_CONF="$tmp/softhsm2.conf" CHERRY_HSM_PIN=123456
softhsm2-util --init-token --free --label "Cherry Secure Boot" --so-pin 12345678 --pin "$CHERRY_HSM_PIN" >/dev/null
keys=$tmp/keys

token() {
	sb "$@" --module "$module" --token "Cherry Secure Boot"
}

# privkeys: the labels of the token's private keys, on one line.
privkeys() {
	pkcs11-tool --module "$module" --token-label "Cherry Secure Boot" --login --pin "$CHERRY_HSM_PIN" \
		--list-objects --type privkey 2>/dev/null | sed -n 's/^ *label: *//p' | tr '\n' ' '
}
# run_deletes: run the pkcs11-tool commands init printed, with the PIN.
run_deletes() {
	printf '%s\n' "$out" | grep '^  pkcs11-tool ' >"$tmp/deletes" || die "no delete commands in: $out"
	while read -r cmd; do
		eval "$cmd --pin \"\$CHERRY_HSM_PIN\"" >/dev/null 2>&1 || die "failed: $cmd"
	done <"$tmp/deletes"
}

sb check
ok
case $out in *"no keys"*) ;; *) die "an empty directory: $out" ;; esac

# A pkcs11-tool that cannot sign, as with the token pulled out: init stops after
# the PK's key, before its certificate.
mkdir "$tmp/bin"
cat >"$tmp/bin/pkcs11-tool" <<EOF
#!/bin/sh
for arg; do
	[ "\$arg" != --sign ] || { echo "error: failing as told" >&2; exit 1; }
done
exec $(command -v pkcs11-tool) "\$@"
EOF
chmod +x "$tmp/bin/pkcs11-tool"
PATH=$tmp/bin:$PATH token init
refused "init stopped, and removed the files it wrote"
[ "$(privkeys)" = "cherry-pk " ] || die "init stopped with keys: $(privkeys)"
[ -z "$(ls -A "$keys")" ] || die "a stopped init left files: $(ls -A "$keys")"
case $out in
*"--delete-object --type privkey --id $(printf cherry-pk | od -An -tx1 | tr -d ' \n') --label cherry-pk"*) ;;
*) die "no command deletes cherry-pk: $out" ;;
esac
token init
refused "the token holds cherry-pk already"
run_deletes
[ -z "$(privkeys)" ] || die "the printed commands left keys: $(privkeys)"

token init
ok
for f in PK.crt PK.auth KEK.crt KEK.auth db.crt db.auth; do
	[ -s "$keys/$f" ] || die "init wrote no $f"
done
[ "$(find "$keys" -type f | wc -l)" -eq 6 ] || die "init wrote more: $(find "$keys" -type f)"
sb check
ok
for var in PK KEK db; do
	openssl x509 -in "$keys/$var.crt" -noout -subject | grep -q "Cherry" || die "$var.crt has no Cherry subject"
done
openssl x509 -in "$keys/db.crt" -noout -ext basicConstraints | grep -q "CA:TRUE" || die "db.crt is not a CA"

token init
refused "exist already"
mv "$keys" "$tmp/first"
token init
refused "the token holds cherry-pk, cherry-kek, cherry-db already"
[ ! -e "$keys/PK.auth" ] || die "init over the token's keys wrote files"
mv "$tmp/first" "$keys"

# issue checks the PIN, that the token holds the CA's key, and that this is the key
# of db.crt, before the key goes out or release.crt is written.
CHERRY_HSM_PIN=999999 token issue --key-out "$tmp/release.key"
refused "cannot log in to the token"
refused "CKR_PIN_INCORRECT"
softhsm2-util --init-token --free --label Other --so-pin 12345678 --pin "$CHERRY_HSM_PIN" >/dev/null
sb issue --module "$module" --token Other --key-out "$tmp/release.key"
refused "the token holds no cherry-db"
pkcs11-tool --module "$module" --token-label Other --login --pin "$CHERRY_HSM_PIN" --keypairgen \
	--key-type rsa:2048 --label cherry-db --id "$(printf cherry-db | od -An -tx1 | tr -d ' \n')" >/dev/null 2>&1
sb issue --module "$module" --token Other --key-out "$tmp/release.key"
refused "the token's cherry-db is not the key of"
if [ -e "$tmp/release.key" ] || [ -e "$keys/release.crt" ]; then
	die "a refused issue wrote the key or release.crt"
fi

TMPDIR=${other:-${TMPDIR:-/tmp}} token issue --key-out "$tmp/release.key"
ok
[ "$(stat -c %a "$tmp/release.key")" = 600 ] || die "the release key is not private to its owner"
[ "$(openssl pkey -in "$tmp/release.key" -pubout)" = "$(openssl x509 -in "$keys/release.crt" -noout -pubkey)" ] ||
	die "release.crt is not the certificate of the release key"
openssl x509 -in "$keys/release.crt" -noout -ext extendedKeyUsage | grep -q "Code Signing" ||
	die "release.crt is not for code signing"
sb check
ok
case $out in *"issued release.crt"*) ;; *) die "check did not cover release.crt: $out" ;; esac
openssl pkey -in "$pcr/pcr.key" -pubout -out "$keys/pcr.pem"
sb check
ok
case $out in *"issued release.crt; pcr.pem is the PCR key"*) ;; *) die "check did not cover pcr.pem: $out" ;; esac
rm "$keys/pcr.pem"
token issue --key-out "$tmp/release.key"
refused "exists"
set +e
python3 scripts/secureboot.py --dir "$keys" issue --module "$module" --token "Cherry Secure Boot" --key-out - \
	>"$tmp/stdout.key" 2>/dev/null
rc=$?
set -e
ok
openssl pkey -in "$tmp/stdout.key" -noout || die "issue --key-out - printed no key"

# A leaked release key: revoke puts its certificate in dbx.auth, signed by the
# KEK; check refuses to go on with that key, and issue replaces it. A second
# leak adds to the list, a certificate the CA didn't issue is refused, as is
# one revoked already.
cp "$keys/release.crt" "$tmp/leaked-1.crt"
token revoke "$tmp/leaked-1.crt"
ok
[ -s "$keys/dbx.auth" ] || die "revoke wrote no dbx.auth"
case $out in *"1 revoked certificate(s)"*"issue a new release key"*) ;; *) die "revoke did not say to issue a new key: $out" ;; esac
sb check
refused "release.crt is revoked by dbx.auth"
token revoke "$tmp/leaked-1.crt"
refused "is revoked already"
openssl req -new -x509 -newkey rsa:2048 -nodes -keyout /dev/null -subj "/CN=not the CA's/" -days 1 \
	-out "$tmp/foreign.crt" 2>/dev/null
token revoke "$tmp/foreign.crt"
refused "not a certificate db.crt issued"
rm "$tmp/release.key"
token issue --key-out "$tmp/release.key"
ok
sb check
ok
case $out in *"issued release.crt; dbx.auth revokes 1 release certificate(s)"*) ;; *) die "check did not count the revocation: $out" ;; esac
cp "$keys/release.crt" "$tmp/leaked-2.crt"
token revoke "$tmp/leaked-2.crt"
ok
rm "$tmp/release.key"
token issue --key-out "$tmp/release.key"
ok
sb check
ok
case $out in *"dbx.auth revokes 2 release certificate(s)"*) ;; *) die "check did not count both revocations: $out" ;; esac
python3 - "$keys" "$tmp" <<'EOF'
import sys
sys.path.insert(0, "scripts")
import secureboot as sb  # noqa: E402

keys, tmp = sys.argv[1:]
revoked = sb.revoked_certificates(f"{keys}/dbx.auth")
leaked = [sb.pem_to_der(f"{tmp}/leaked-{i}.crt") for i in (1, 2)]
assert revoked == leaked, "dbx.auth does not revoke the two leaked certificates, in order"
with open(f"{keys}/dbx.auth", "rb") as f:
    timestamp = sb.parse_auth2(f.read())[0]
with open(f"{keys}/db.auth", "rb") as f:
    assert timestamp > sb.parse_auth2(f.read())[0], "dbx.auth is not later than db.auth"
EOF
n=$((n + 1))

cp -a "$keys" "$tmp/good"
python3 -c 'import sys; p = sys.argv[1]; b = bytearray(open(p, "rb").read()); b[-1] ^= 1; open(p, "wb").write(b)' \
	"$keys/db.auth"
sb check
refused "db.auth: its signature does not verify with KEK.crt"
rm -rf "$keys" && cp -a "$tmp/good" "$keys"
cp "$keys/PK.auth" "$keys/KEK.auth"
sb check
refused "KEK.auth: its signature does not verify with PK.crt"
rm -rf "$keys" && cp -a "$tmp/good" "$keys"
openssl req -new -x509 -newkey rsa:2048 -nodes -keyout /dev/null -subj "/CN=not the CA's/" -days 1 \
	-out "$keys/release.crt" 2>/dev/null
sb check
refused "release.crt: not issued by db.crt"
rm "$keys/release.crt" "$keys/db.crt"
sb check
refused "lacks db.crt"
rm -rf "$keys" && cp -a "$tmp/good" "$keys"
python3 -c 'import sys; p = sys.argv[1]; b = bytearray(open(p, "rb").read()); b[-1] ^= 1; open(p, "wb").write(b)' \
	"$keys/dbx.auth"
sb check
refused "dbx.auth: its signature does not verify with KEK.crt"
rm -rf "$keys" && cp -a "$tmp/good" "$keys"
cp "$keys/db.auth" "$keys/dbx.auth"
sb check
refused "dbx.auth: its signature does not verify with KEK.crt"

echo "ceremony: $n cases passed"
