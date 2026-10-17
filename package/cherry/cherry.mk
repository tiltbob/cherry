################################################################################
#
# cherry
#
################################################################################

# Cherry's own, no source: the cherry user; cherry.service, which runs
# OpenChamber's server as that user (see package/openchamber for the server,
# and its openchamber.service for the same as the openchamber user);
# cherry-connect-url.service, which shows the link that pairs another
# OpenChamber app with it on the console once it is online; the user's
# container privileges: polkit rules for machinectl, cherry-bootstrap@.service,
# which creates containers for it as root, and cherry-image@.service, which
# builds images for it as root; the environments built on them: the verbs
# (environments.js) and the profiles they build from (profiles/, to
# /usr/lib/cherry/profiles); the OpenCode agent's tools for them, in the
# system configuration directory /etc/opencode: the MCP server
# cherry-machines-mcp, run on Bun, and the machine-images skill; and
# OpenChamber's Environments extension (openchamber-extension/), installed as
# a built-in of the OpenChamber package, which gets its grants without a
# click: the registry names it (openchamber-registry.json).
CHERRY_DEPENDENCIES = openchamber
CHERRY_USERS = cherry -1 cherry -1 * /var/lib/cherry - - Cherry
CHERRY_OPENCHAMBER_EXTENSIONS = /usr/lib/openchamber/node_modules/@openchamber/web/server/built-in-extensions

define CHERRY_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/50-cherry.rules \
		$(TARGET_DIR)/usr/share/polkit-1/rules.d/50-cherry.rules
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/environments.js \
		$(TARGET_DIR)/usr/lib/cherry/environments.js
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/cherry-machines-mcp.js \
		$(TARGET_DIR)/usr/lib/cherry/machines-mcp.js
	rm -rf $(TARGET_DIR)/usr/lib/cherry/profiles
	mkdir -p $(TARGET_DIR)/usr/lib/cherry/profiles
	cp -dpr $(CHERRY_PKGDIR)/profiles/. $(TARGET_DIR)/usr/lib/cherry/profiles/
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/opencode.json \
		$(TARGET_DIR)/etc/opencode/opencode.json
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/machine-images.md \
		$(TARGET_DIR)/etc/opencode/skills/machine-images.md
	rm -rf $(TARGET_DIR)$(CHERRY_OPENCHAMBER_EXTENSIONS)/cherry-environments
	mkdir -p $(TARGET_DIR)$(CHERRY_OPENCHAMBER_EXTENSIONS)
	cp -dpr $(CHERRY_PKGDIR)/openchamber-extension \
		$(TARGET_DIR)$(CHERRY_OPENCHAMBER_EXTENSIONS)/cherry-environments
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/openchamber-registry.json \
		$(TARGET_DIR)$(CHERRY_OPENCHAMBER_EXTENSIONS)/registry.json
endef

define CHERRY_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/cherry.service \
		$(TARGET_DIR)/usr/lib/systemd/system/cherry.service
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/cherry-connect-url.service \
		$(TARGET_DIR)/usr/lib/systemd/system/cherry-connect-url.service
	$(INSTALL) -D -m 0755 $(CHERRY_PKGDIR)/cherry-connect-url \
		$(TARGET_DIR)/usr/libexec/cherry-connect-url
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/cherry-bootstrap@.service \
		$(TARGET_DIR)/usr/lib/systemd/system/cherry-bootstrap@.service
	$(INSTALL) -D -m 0755 $(CHERRY_PKGDIR)/cherry-bootstrap \
		$(TARGET_DIR)/usr/libexec/cherry-bootstrap
	$(INSTALL) -D -m 0644 $(CHERRY_PKGDIR)/cherry-image@.service \
		$(TARGET_DIR)/usr/lib/systemd/system/cherry-image@.service
	$(INSTALL) -D -m 0755 $(CHERRY_PKGDIR)/cherry-image \
		$(TARGET_DIR)/usr/libexec/cherry-image
	$(INSTALL) -D -m 0755 $(CHERRY_PKGDIR)/cherry-image-setup \
		$(TARGET_DIR)/usr/libexec/cherry-image-setup
endef

$(eval $(generic-package))
