include $(sort $(wildcard $(BR2_EXTERNAL_CHERRY_PATH)/package/*/*.mk))

# wget picks gnutls for TLS when both gnutls (pacman) and openssl are on the
# image, but its configure still builds NTLM auth against OpenSSL's DES/MD4
# without linking libcrypto, so the link fails. Cherry never needs NTLM.
# (Configure options are expanded when the rule runs, so appending here works.)
WGET_CONF_OPTS += --disable-ntlm
