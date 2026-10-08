################################################################################
#
# openchamber
#
################################################################################

OPENCHAMBER_VERSION = 2.2.0
OPENCHAMBER_SITE = https://registry.npmjs.org/@openchamber/web/-
# Upstream's npm package: the server, its command line and the built web UI,
# published to run on Node.js or Bun; upstream's own container runs it on Bun,
# and so does Cherry. Its dependencies are npm tarballs too, one download
# each: npm-modules.list names them with their install paths, and
# openchamber.hash their sha512 from the npm registry. Both are generated from
# npm's lockfile for this version by scripts/update_packages.py, which also
# bumps the version. The build itself needs neither node nor npm: the tarballs
# are unpacked where npm would put them.
OPENCHAMBER_SOURCE = web-$(OPENCHAMBER_VERSION).tgz
OPENCHAMBER_NPM_MODULES = $(BR2_EXTERNAL_CHERRY_PATH)/package/openchamber/npm-modules.list
OPENCHAMBER_EXTRA_DOWNLOADS = \
	https://raw.githubusercontent.com/openchamber/openchamber/v$(OPENCHAMBER_VERSION)/LICENSE \
	$(shell awk '!/^\#/ && NF { print $$2 }' $(OPENCHAMBER_NPM_MODULES))
OPENCHAMBER_LICENSE = MIT (OpenChamber), MIT, ISC, Apache-2.0, BSD and other licenses (bundled npm modules)
OPENCHAMBER_LICENSE_FILES = LICENSE

# The tree as npm lays it out: @openchamber/web and its dependencies side by
# side in node_modules/, a dependency nested under its parent where versions
# conflict. Every npm tarball holds its files under one directory (usually
# package/).
define OPENCHAMBER_EXTRACT_CMDS
	mkdir -p $(@D)/node_modules/@openchamber/web
	$(ZCAT) $(OPENCHAMBER_DL_DIR)/$(OPENCHAMBER_SOURCE) | \
		$(TAR) --strip-components=1 -C $(@D)/node_modules/@openchamber/web $(TAR_OPTIONS) -
	awk '!/^#/ && NF { print $$1, $$2 }' $(OPENCHAMBER_NPM_MODULES) | \
	while read -r path url; do \
		mkdir -p $(@D)/node_modules/$$path && \
		$(ZCAT) $(OPENCHAMBER_DL_DIR)/$${url##*/} | \
			$(TAR) --strip-components=1 -C $(@D)/node_modules/$$path $(TAR_OPTIONS) - || exit 1; \
	done
	cp $(OPENCHAMBER_DL_DIR)/LICENSE $(@D)/LICENSE
endef

# Prebuilt native code for other platforms, shipped in the same packages:
# node-pty's terminals (Bun uses bun-pty's) and bun-pty's own library.
define OPENCHAMBER_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)/usr/lib/openchamber
	mkdir -p $(TARGET_DIR)/usr/lib/openchamber
	cp -a $(@D)/node_modules $(TARGET_DIR)/usr/lib/openchamber/
	cd $(TARGET_DIR)/usr/lib/openchamber/node_modules/node-pty/prebuilds && \
		rm -rf darwin-arm64 darwin-x64 linux-arm64 win32-arm64 win32-x64
	cd $(TARGET_DIR)/usr/lib/openchamber/node_modules/bun-pty/rust-pty/target/release && \
		rm -f *arm64* *musl* *.dylib *.dll
	$(INSTALL) -D -m 0755 $(OPENCHAMBER_PKGDIR)/openchamber $(TARGET_DIR)/usr/bin/openchamber
endef

define OPENCHAMBER_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(OPENCHAMBER_PKGDIR)/openchamber.service \
		$(TARGET_DIR)/usr/lib/systemd/system/openchamber.service
	$(INSTALL) -D -m 0755 $(OPENCHAMBER_PKGDIR)/openchamber-serve \
		$(TARGET_DIR)/usr/libexec/openchamber-serve
endef

$(eval $(generic-package))
