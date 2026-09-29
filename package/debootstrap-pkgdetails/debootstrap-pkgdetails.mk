################################################################################
#
# debootstrap-pkgdetails
#
################################################################################

# Only pkgdetails.c is built: debian-installer's replacement for the Perl
# helpers in debootstrap, so the target needs no Perl.
DEBOOTSTRAP_PKGDETAILS_VERSION = 1.230
DEBOOTSTRAP_PKGDETAILS_SITE = https://snapshot.debian.org/archive/debian/20260918T023724Z/pool/main/b/base-installer
DEBOOTSTRAP_PKGDETAILS_SOURCE = base-installer_$(DEBOOTSTRAP_PKGDETAILS_VERSION).tar.xz
DEBOOTSTRAP_PKGDETAILS_LICENSE = GPL-2.0
DEBOOTSTRAP_PKGDETAILS_LICENSE_FILES = debian/copyright

define DEBOOTSTRAP_PKGDETAILS_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -D_GNU_SOURCE \
		-o $(@D)/pkgdetails $(@D)/pkgdetails.c
endef

define DEBOOTSTRAP_PKGDETAILS_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/pkgdetails \
		$(TARGET_DIR)/usr/lib/debootstrap/pkgdetails
endef

$(eval $(generic-package))
