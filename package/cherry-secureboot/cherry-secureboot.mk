################################################################################
#
# cherry-secureboot
#
################################################################################

# Cherry's own, no source: cherry-secureboot.service, and the signed variable
# updates it enrolls in setup mode, from the keys directory (all three, or
# none: then the unit doesn't start). They are public: certificates, and the
# token's signatures over them. A change to them needs
# `make br-cherry-secureboot-reinstall`.
CHERRY_SECUREBOOT_KEYS = $(call qstrip,$(BR2_PACKAGE_CHERRY_SECUREBOOT_KEYS))

define CHERRY_SECUREBOOT_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)/usr/share/cherry/secureboot
	if [ -e $(CHERRY_SECUREBOOT_KEYS)/PK.auth ]; then \
		for var in PK KEK db; do \
			$(INSTALL) -D -m 0644 $(CHERRY_SECUREBOOT_KEYS)/$$var.auth \
				$(TARGET_DIR)/usr/share/cherry/secureboot/$$var.auth || exit 1; \
		done; \
	fi
endef

define CHERRY_SECUREBOOT_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(CHERRY_SECUREBOOT_PKGDIR)/cherry-secureboot.service \
		$(TARGET_DIR)/usr/lib/systemd/system/cherry-secureboot.service
	$(INSTALL) -D -m 0755 $(CHERRY_SECUREBOOT_PKGDIR)/cherry-secureboot \
		$(TARGET_DIR)/usr/libexec/cherry-secureboot
endef

$(eval $(generic-package))
