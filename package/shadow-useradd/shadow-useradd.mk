################################################################################
#
# shadow-useradd
#
################################################################################

# The same source as Buildroot's shadow package.
SHADOW_USERADD_VERSION = 4.18.0
SHADOW_USERADD_SITE = https://github.com/shadow-maint/shadow/releases/download/$(SHADOW_USERADD_VERSION)
SHADOW_USERADD_SOURCE = shadow-$(SHADOW_USERADD_VERSION).tar.xz
SHADOW_USERADD_DL_SUBDIR = shadow
SHADOW_USERADD_LICENSE = BSD-3-Clause
SHADOW_USERADD_LICENSE_FILES = COPYING
SHADOW_USERADD_CPE_ID_VENDOR = debian
SHADOW_USERADD_CPE_ID_PRODUCT = shadow
SHADOW_USERADD_DEPENDENCIES = $(TARGET_NLS_DEPENDENCIES)
SHADOW_USERADD_CONF_ENV = LIBS=$(TARGET_NLS_LIBS)
SHADOW_USERADD_CONF_OPTS = \
	--disable-man \
	--disable-account-tools-setuid \
	--disable-subordinate-ids \
	--without-acl \
	--without-attr \
	--without-audit \
	--without-btrfs \
	--without-libbsd \
	--without-libcrack \
	--without-libpam \
	--without-nscd \
	--without-selinux \
	--without-skey \
	--without-sssd \
	--without-su \
	--without-tcb

ifeq ($(BR2_PACKAGE_LIBXCRYPT),y)
SHADOW_USERADD_DEPENDENCIES += libxcrypt
endif

SHADOW_USERADD_PROGS = groupadd useradd usermod

define SHADOW_USERADD_INSTALL_TARGET_CMDS
	$(foreach p,$(SHADOW_USERADD_PROGS),
		$(INSTALL) -D -m 0755 $(@D)/src/$(p) $(TARGET_DIR)/usr/sbin/$(p))
endef

$(eval $(autotools-package))
