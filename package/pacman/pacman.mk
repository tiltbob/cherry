################################################################################
#
# pacman
#
################################################################################

PACMAN_VERSION = 7.1.0
PACMAN_SITE = https://gitlab.archlinux.org/-/project/19637/uploads/f7f47e176b9ad29a31a0704e5df8fc38
PACMAN_SOURCE = pacman-$(PACMAN_VERSION).tar.xz
PACMAN_LICENSE = GPL-2.0+
PACMAN_LICENSE_FILES = COPYING
PACMAN_DEPENDENCIES = host-pkgconf libarchive libcurl libgpgme openssl

# Scriptlets and ldconfig run chrooted in the Arch root, so use Arch's paths.
PACMAN_CONF_OPTS = \
	-Ddoc=disabled \
	-Ddoxygen=disabled \
	-Dcurl=enabled \
	-Dgpgme=enabled \
	-Dcrypto=openssl \
	-Dfile-seccomp=disabled \
	-Dscriptlet-shell=/usr/bin/bash \
	-Dldconfig=/usr/bin/ldconfig

ifeq ($(BR2_SYSTEM_ENABLE_NLS),y)
PACMAN_CONF_OPTS += -Di18n=true
PACMAN_DEPENDENCIES += $(TARGET_NLS_DEPENDENCIES)
else
PACMAN_CONF_OPTS += -Di18n=false
endif

ifeq ($(BR2_PACKAGE_LIBSECCOMP),y)
PACMAN_DEPENDENCIES += libseccomp
endif

# meson writes the build host's bash path into the scripts' shebangs. Only
# pacman-key (and the libmakepkg helpers it sources) is useful on the target.
define PACMAN_TRIM_TARGET
	$(SED) '1s|^#!.*|#!/usr/bin/bash|' $(TARGET_DIR)/usr/bin/pacman-key
	rm -f $(addprefix $(TARGET_DIR)/usr/bin/,makepkg makepkg-template \
		pacman-db-upgrade repo-add repo-remove repo-elephant testpkg)
	rm -rf $(TARGET_DIR)/etc/makepkg.conf $(TARGET_DIR)/etc/makepkg.conf.d \
		$(TARGET_DIR)/etc/makepkg.d $(TARGET_DIR)/usr/share/makepkg-template \
		$(TARGET_DIR)/usr/share/pacman/*.proto \
		$(TARGET_DIR)/usr/share/pacman/proto.install
	find $(TARGET_DIR)/usr/share/makepkg -mindepth 1 -maxdepth 1 \
		! -name util -exec rm -rf {} +
endef
PACMAN_POST_INSTALL_TARGET_HOOKS += PACMAN_TRIM_TARGET

# Arch Linux repositories. pacstrap hard-codes /etc/pacman.conf and
# /etc/pacman.d/mirrorlist; /etc is read-only, so the keyring lives in /var.
define PACMAN_INSTALL_CONFIG
	$(INSTALL) -D -m 0644 $(PACMAN_PKGDIR)/pacman.conf $(TARGET_DIR)/etc/pacman.conf
	$(INSTALL) -D -m 0644 $(PACMAN_PKGDIR)/mirrorlist $(TARGET_DIR)/etc/pacman.d/mirrorlist
	ln -sfn /var/lib/pacman/gnupg $(TARGET_DIR)/etc/pacman.d/gnupg
endef
PACMAN_POST_INSTALL_TARGET_HOOKS += PACMAN_INSTALL_CONFIG

define PACMAN_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(PACMAN_PKGDIR)/pacman-init.service \
		$(TARGET_DIR)/usr/lib/systemd/system/pacman-init.service
endef

$(eval $(meson-package))
