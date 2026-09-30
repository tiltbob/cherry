################################################################################
#
# archlinux-keyring
#
################################################################################

# Release tarballs ship the built keyring, so no sq/keyringctl is needed.
ARCHLINUX_KEYRING_VERSION = 20260909
ARCHLINUX_KEYRING_SITE = https://gitlab.archlinux.org/-/project/19588/uploads/6693c64fcaa9c280a4785d7a189bff9e
ARCHLINUX_KEYRING_LICENSE = GPL-3.0+

define ARCHLINUX_KEYRING_INSTALL_TARGET_CMDS
	$(foreach f,archlinux.gpg archlinux-trusted archlinux-revoked,
		$(INSTALL) -D -m 0644 $(@D)/$(f) \
			$(TARGET_DIR)/usr/share/pacman/keyrings/$(f))
endef

$(eval $(generic-package))
