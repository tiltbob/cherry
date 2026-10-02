################################################################################
#
# cherry-stage1
#
################################################################################

CHERRY_STAGE1_SITE = $(CHERRY_STAGE1_PKGDIR)/src
CHERRY_STAGE1_SITE_METHOD = local
CHERRY_STAGE1_INSTALL_TARGET = NO
CHERRY_STAGE1_INSTALL_IMAGES = YES

# Static: the initramfs has no C library.
define CHERRY_STAGE1_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -static \
		-o $(@D)/stage1 $(@D)/stage1.c
endef

define CHERRY_STAGE1_INSTALL_IMAGES_CMDS
	$(INSTALL) -D -m 0755 $(@D)/stage1 $(BINARIES_DIR)/stage1
endef

$(eval $(generic-package))
