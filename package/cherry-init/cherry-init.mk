################################################################################
#
# cherry-init
#
################################################################################

CHERRY_INIT_SITE = $(BR2_EXTERNAL_CHERRY_PATH)/package/cherry-init/src
CHERRY_INIT_SITE_METHOD = local
CHERRY_INIT_LICENSE = MIT
CHERRY_INIT_INSTALL_TARGET = NO
CHERRY_INIT_INSTALL_IMAGES = YES

define CHERRY_INIT_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -static -Wall -Wextra -Werror \
		-o $(@D)/init $(@D)/init.c
endef

define CHERRY_INIT_INSTALL_IMAGES_CMDS
	$(INSTALL) -D -m 0755 $(@D)/init $(BINARIES_DIR)/cherry-init
endef

$(eval $(generic-package))
