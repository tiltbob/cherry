################################################################################
#
# opencode
#
################################################################################

OPENCODE_VERSION = 2.0.25
OPENCODE_SITE = https://registry.npmjs.org/@opencode/cli-linux-x64-baseline/-
# Upstream's prebuilt binary: OpenCode is a TypeScript program compiled into a
# single executable with Bun, and building it needs Bun and its npm
# dependencies on the host. OpenCode 2 is released on npm, as one platform
# package per binary (@opencode/cli pulls in the matching one). The "baseline"
# x86_64 build doesn't need AVX2, so it also runs on VMs whose CPU model lacks
# it (QEMU's qemu64, x86-64-v2).
OPENCODE_SOURCE = cli-linux-x64-baseline-$(OPENCODE_VERSION).tgz
# The release doesn't carry the license; take it from the tagged source.
OPENCODE_EXTRA_DOWNLOADS = https://raw.githubusercontent.com/anomalyco/opencode/v$(OPENCODE_VERSION)/LICENSE
OPENCODE_LICENSE = MIT
OPENCODE_LICENSE_FILES = LICENSE

define OPENCODE_ADD_LICENSE
	cp $(OPENCODE_DL_DIR)/LICENSE $(@D)/LICENSE
endef
OPENCODE_POST_EXTRACT_HOOKS += OPENCODE_ADD_LICENSE

# A Bun single-file executable. Stripped, it no longer finds the program
# embedded in it and runs as a bare Bun (`opencode --version` prints Bun's
# version), so the defconfig's BR2_STRIP_EXCLUDE_FILES lists opencode, and
# post-build.sh checks that the installed binary is still the extracted one.
define OPENCODE_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/bin/opencode $(TARGET_DIR)/usr/bin/opencode
endef

# The server as a service, with its user: opencode-serve gives it its password
# for the boot.
ifeq ($(BR2_PACKAGE_OPENCODE_SERVICE),y)
OPENCODE_USERS = opencode -1 opencode -1 * /var/lib/opencode - - OpenCode server
define OPENCODE_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(OPENCODE_PKGDIR)/opencode.service \
		$(TARGET_DIR)/usr/lib/systemd/system/opencode.service
	$(INSTALL) -D -m 0755 $(OPENCODE_PKGDIR)/opencode-serve \
		$(TARGET_DIR)/usr/libexec/opencode-serve
endef
endif

$(eval $(generic-package))
