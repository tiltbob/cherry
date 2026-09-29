################################################################################
#
# arch-install-scripts
#
################################################################################

ARCH_INSTALL_SCRIPTS_VERSION = 31
ARCH_INSTALL_SCRIPTS_SITE = https://gitlab.archlinux.org/archlinux/arch-install-scripts/-/archive/v$(ARCH_INSTALL_SCRIPTS_VERSION)
ARCH_INSTALL_SCRIPTS_SOURCE = arch-install-scripts-v$(ARCH_INSTALL_SCRIPTS_VERSION).tar.gz
ARCH_INSTALL_SCRIPTS_LICENSE = GPL-2.0
ARCH_INSTALL_SCRIPTS_LICENSE_FILES = COPYING
ARCH_INSTALL_SCRIPTS_DEPENDENCIES = host-m4

# The default target also builds the man pages, which need asciidoc.
ARCH_INSTALL_SCRIPTS_PROGS = arch-chroot pacstrap

define ARCH_INSTALL_SCRIPTS_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) $(ARCH_INSTALL_SCRIPTS_PROGS)
endef

define ARCH_INSTALL_SCRIPTS_INSTALL_TARGET_CMDS
	$(foreach p,$(ARCH_INSTALL_SCRIPTS_PROGS),
		$(INSTALL) -D -m 0755 $(@D)/$(p) $(TARGET_DIR)/usr/bin/$(p))
endef

$(eval $(generic-package))
