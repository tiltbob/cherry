################################################################################
#
# cherry-pit
#
################################################################################

CHERRY_PIT_SITE = $(CHERRY_PIT_PKGDIR)/src
CHERRY_PIT_SITE_METHOD = local
CHERRY_PIT_INSTALL_TARGET = NO
CHERRY_PIT_INSTALL_IMAGES = YES

# Static: the initramfs has no C library.
define CHERRY_PIT_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -static \
		-o $(@D)/cherry-pit $(@D)/cherry-pit.c
endef

define CHERRY_PIT_INSTALL_IMAGES_CMDS
	$(INSTALL) -D -m 0755 $(@D)/cherry-pit $(BINARIES_DIR)/cherry-pit
endef

$(eval $(generic-package))
