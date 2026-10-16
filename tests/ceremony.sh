#!/bin/sh
# scripts/secureboot.py makes Cherry's root of trust on a token and checks it:
# run against a SoftHSM token standing in for the Nitrokey, all in a temporary
# directory. init writes the six files and check verifies them; init refuses
# to run again, over the files or over the keys on the token; issue writes
# release.crt and its key, to a new file or stdout; check catches a tampered
# file, a swapped one and a release certificate the CA didn't issue. Skipped
# without softhsm2-util, pkcs11-tool (OpenSC) and OpenSSL's pkcs11-provider.
set -eu
cd "$(dirname "$0")/.."

module=
for m in /usr/lib/softhsm/libsofthsm2.so /usr/lib64/pkcs11/libsofthsm2.so /usr/lib/x86_64-linux-gnu/softhsm/libsofthsm2.so; do
	[ -e "$m" ] && module=$m && break
done
if [ -z "$module" ] || ! command -v softhsm2-util >/dev/null || ! command -v pkcs11-tool >/dev/null ||
	! PKCS11_PROVIDER_MODULE=$module openssl list -providers -provider pkcs11 >/dev/null 2>&1; then
	echo "ceremony: SKIPPED, needs softhsm2, opensc and pkcs11-provider (scripts/secureboot.py init and issue unchecked)"
	exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/tokens"
printf 'directories.tokendir = %s\nobjectstore.backend = file\nlog.level = ERROR\n' "$tmp/tokens" >"$tmp/softhsm2.conf"
export SOFTHSM2_CONF="$tmp/softhsm2.conf" CHERRY_HSM_PIN=123456
softhsm2-util --init-token --free --label "Cherry Secure Boot" --so-pin 12345678 --pin "$CHERRY_HSM_PIN" >/dev/null
keys=$tmp/keys

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
token() {
	sb "$@" --module "$module" --token "Cherry Secure Boot"
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

sb check
ok
case $out in *"no keys"*) ;; *) die "an empty directory: $out" ;; esac

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

token issue --key-out "$tmp/release.key"
ok
[ "$(stat -c %a "$tmp/release.key")" = 600 ] || die "the release key is not private to its owner"
[ "$(openssl pkey -in "$tmp/release.key" -pubout)" = "$(openssl x509 -in "$keys/release.crt" -noout -pubkey)" ] ||
	die "release.crt is not the certificate of the release key"
openssl x509 -in "$keys/release.crt" -noout -ext extendedKeyUsage | grep -q "Code Signing" ||
	die "release.crt is not for code signing"
sb check
ok
case $out in *"issued release.crt"*) ;; *) die "check did not cover release.crt: $out" ;; esac
token issue --key-out "$tmp/release.key"
refused "exists"
set +e
python3 scripts/secureboot.py --dir "$keys" issue --module "$module" --key-out - >"$tmp/stdout.key" 2>/dev/null
rc=$?
set -e
ok
openssl pkey -in "$tmp/stdout.key" -noout || die "issue --key-out - printed no key"

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

echo "ceremony: $n cases passed"
