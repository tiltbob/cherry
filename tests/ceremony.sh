#!/bin/sh
# scripts/secureboot.py makes Cherry's root of trust on a token and checks it:
# run against a SoftHSM token standing in for the Nitrokey, all in a temporary
# directory. Without OpenSSL's provider, init stops before it touches the
# token; stopped after a key, it removes its files and prints the commands that
# delete what it generated, which then refuses to run over until they ran.
# issue refuses a wrong PIN, a token without the CA's key, and one whose key of
# that name is not the CA's, before it hands out a key.
# init writes the six files and check verifies them; init refuses
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

mkdir "$tmp/no-modules"
OPENSSL_MODULES=$tmp/no-modules token init
refused "OpenSSL cannot load its PKCS#11 provider: install pkcs11-provider"
[ -z "$(privkeys)" ] || die "init without the provider generated keys: $(privkeys)"
[ ! -e "$keys" ] || die "init without the provider wrote files: $(ls -A "$keys")"

# An openssl that makes no certificate: init stops after the PK's key.
mkdir "$tmp/bin"
cat >"$tmp/bin/openssl" <<EOF
#!/bin/sh
[ "\$1" != req ] || { echo "req: failing as told" >&2; exit 1; }
exec $(command -v openssl) "\$@"
EOF
chmod +x "$tmp/bin/openssl"
PATH=$tmp/bin:$PATH token init
refused "init stopped, and removed the files it wrote"
[ "$(privkeys)" = "cherry-pk " ] || die "init stopped with keys: $(privkeys)"
[ -z "$(ls -A "$keys")" ] || die "a stopped init left files: $(ls -A "$keys")"
case $out in
*"--delete-object --type privkey --label cherry-pk"*) ;;
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

# issue checks the PIN and that the token holds the CA's key; OpenSSL, that this is
# the key of db.crt; all before the key goes out or release.crt is written.
CHERRY_HSM_PIN=999999 token issue --key-out "$tmp/release.key"
refused "cannot log in to the token"
refused "CKR_PIN_INCORRECT"
softhsm2-util --init-token --free --label Other --so-pin 12345678 --pin "$CHERRY_HSM_PIN" >/dev/null
sb issue --module "$module" --token Other --key-out "$tmp/release.key"
refused "the token holds no cherry-db"
pkcs11-tool --module "$module" --token-label Other --login --pin "$CHERRY_HSM_PIN" --keypairgen \
	--key-type rsa:2048 --label cherry-db --id "$(printf cherry-db | od -An -tx1 | tr -d ' \n')" >/dev/null 2>&1
sb issue --module "$module" --token Other --key-out "$tmp/release.key"
refused "Issuer CA certificate and key do not match"
if [ -e "$tmp/release.key" ] || [ -e "$keys/release.crt" ]; then
	die "a refused issue wrote the key or release.crt"
fi

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
python3 scripts/secureboot.py --dir "$keys" issue --module "$module" --token "Cherry Secure Boot" --key-out - \
	>"$tmp/stdout.key" 2>/dev/null
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
