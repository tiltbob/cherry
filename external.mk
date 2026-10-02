include $(sort $(wildcard $(BR2_EXTERNAL_CHERRY_PATH)/package/*/*.mk))

# wget picks gnutls for TLS when both gnutls (pacman) and openssl are on the
# image, but its configure still builds NTLM auth against OpenSSL's DES/MD4
# without linking libcrypto, so the link fails. Cherry never needs NTLM.
# (Configure options are expanded when the rule runs, so appending here works.)
WGET_CONF_OPTS += --disable-ntlm

# The root filesystem is a zstd-compressed EROFS image, but Buildroot builds
# host-erofs-utils --without-libzstd; the last option wins. host-zstd is built
# first, as host-ccache needs it and BR2_CCACHE makes every package depend on
# host-ccache. (A dependency appended here would come too late to count.)
HOST_EROFS_UTILS_CONF_OPTS += --with-libzstd
