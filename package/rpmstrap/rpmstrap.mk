################################################################################
#
# rpmstrap
#
################################################################################

define RPMSTRAP_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(RPMSTRAP_PKGDIR)/rpmstrap $(TARGET_DIR)/usr/bin/rpmstrap
	cd $(RPMSTRAP_PKGDIR)/profiles && for f in */*; do \
		$(INSTALL) -D -m 0644 $$f $(TARGET_DIR)/usr/share/rpmstrap/$$f || exit 1; \
	done
endef

$(eval $(generic-package))
