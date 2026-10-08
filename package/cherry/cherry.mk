################################################################################
#
# cherry
#
################################################################################

# Cherry's own, no source: the cherry user; cherry.service, which runs
# OpenChamber's server as that user (see package/openchamber for the server,
# and its openchamber.service for the same as the openchamber user); and
# cherry-connect-url.service, which shows the link that pairs another
# OpenChamber app with it on the console once it is online.
CHERRY_DEPENDENCIES = openchamber
CHERRY_USERS = cherry -1 cherry -1 * /var/lib/cherry - - Cherry

define CHERRY_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/cherry.service \
		$(TARGET_DIR)/usr/lib/systemd/system/cherry.service
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/cherry-connect-url.service \
		$(TARGET_DIR)/usr/lib/systemd/system/cherry-connect-url.service
	$(INSTALL) -D -m 0755 $(CHERRY_PKGDIR)/cherry-connect-url \
		$(TARGET_DIR)/usr/libexec/cherry-connect-url
endef

$(eval $(generic-package))
