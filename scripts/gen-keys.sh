#!/bin/sh
# Generate a development RAUC signing key and self-signed certificate. The
# certificate doubles as the keyring installed into the image, so only bundles
# signed with this key can be installed on images built with it.
#
# Production images should use a proper PKI: point CHERRY_RAUC_KEY,
# CHERRY_RAUC_CERT and CHERRY_RAUC_KEYRING at it instead.
set -eu

dir=${1:-keys}
key=$dir/dev.key.pem
cert=$dir/dev.cert.pem

if [ -e "$key" ] || [ -e "$cert" ]; then
	echo "keeping existing keys in $dir"
	exit 0
fi

mkdir -p "$dir"
umask 077
openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
	-subj "/O=Cherry/CN=cherry-dev-$(date +%Y%m%d%H%M%S)" \
	-keyout "$key" -out "$cert"
chmod 0644 "$cert"
echo "created $key and $cert"
