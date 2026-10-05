################################################################################
#
# opencode
#
################################################################################

OPENCODE_VERSION = 1.18.34
OPENCODE_SITE = https://github.com/anomalyco/opencode/releases/download/v$(OPENCODE_VERSION)
# Upstream's prebuilt binary: OpenCode is a TypeScript program compiled into a
# single executable with Bun, and building it needs Bun and its npm
# dependencies on the host. The "baseline" x86_64 build doesn't need AVX2, so
# it also runs on VMs whose CPU model lacks it (QEMU's qemu64, x86-64-v2).
# The tarball holds the binary alone, under no directory.
OPENCODE_SOURCE = opencode-linux-x64-baseline.tar.gz
OPENCODE_STRIP_COMPONENTS = 0
# The release doesn't carry the license; take it from the tagged source.
OPENCODE_EXTRA_DOWNLOADS = https://raw.githubusercontent.com/anomalyco/opencode/v$(OPENCODE_VERSION)/LICENSE
OPENCODE_LICENSE = MIT
OPENCODE_LICENSE_FILES = LICENSE
OPENCODE_USERS = opencode -1 opencode -1 * /var/lib/opencode - - OpenCode server

define OPENCODE_ADD_LICENSE
	cp $(OPENCODE_DL_DIR)/LICENSE $(@D)/LICENSE
endef
OPENCODE_POST_EXTRACT_HOOKS += OPENCODE_ADD_LICENSE

# A Bun single-file executable. Stripped, it no longer finds the program
# embedded in it and runs as a bare Bun (`opencode --version` prints Bun's
# version), so the defconfig's BR2_STRIP_EXCLUDE_FILES lists opencode, and
# post-build.sh checks that the installed binary is still the extracted one.
define OPENCODE_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/opencode $(TARGET_DIR)/usr/bin/opencode
endef

define OPENCODE_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(OPENCODE_PKGDIR)/opencode.service \
		$(TARGET_DIR)/usr/lib/systemd/system/opencode.service
endef

$(eval $(generic-package))
