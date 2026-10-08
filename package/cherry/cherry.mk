################################################################################
#
# cherry
#
################################################################################

# Cherry's own, no source: the cherry user, and cherry.service, which runs
# OpenChamber's server as that user (see package/openchamber for the server,
# and its openchamber.service for the same as the openchamber user).
CHERRY_DEPENDENCIES = openchamber
CHERRY_USERS = cherry -1 cherry -1 * /var/lib/cherry - - Cherry

define CHERRY_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/cherry.service \
		$(TARGET_DIR)/usr/lib/systemd/system/cherry.service
endef

$(eval $(generic-package))
