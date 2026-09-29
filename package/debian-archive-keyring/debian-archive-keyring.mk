################################################################################
#
# debian-archive-keyring
#
################################################################################

# The binary package: building the keyrings from source needs jetring and
# gpg on the build host.
DEBIAN_ARCHIVE_KEYRING_VERSION = 2025.1
DEBIAN_ARCHIVE_KEYRING_SITE = https://snapshot.debian.org/archive/debian/20250410T023313Z/pool/main/d/debian-archive-keyring
DEBIAN_ARCHIVE_KEYRING_SOURCE = debian-archive-keyring_$(DEBIAN_ARCHIVE_KEYRING_VERSION)_all.deb
DEBIAN_ARCHIVE_KEYRING_LICENSE = GPL-2.0+ (packaging), public domain (keys)
DEBIAN_ARCHIVE_KEYRING_LICENSE_FILES = usr/share/doc/debian-archive-keyring/copyright
DEBIAN_ARCHIVE_KEYRING_EXTRACT_DEPENDENCIES = $(BR2_XZCAT_HOST_DEPENDENCY)

define DEBIAN_ARCHIVE_KEYRING_EXTRACT_CMDS
	$(HOSTAR) p $(DEBIAN_ARCHIVE_KEYRING_DL_DIR)/$(DEBIAN_ARCHIVE_KEYRING_SOURCE) \
		data.tar.xz | $(XZCAT) | $(TAR) -C $(@D) $(TAR_OPTIONS) - ./usr/share
endef

# The .gpg names are symlinks to the .pgp keyrings.
define DEBIAN_ARCHIVE_KEYRING_INSTALL_TARGET_CMDS
	mkdir -p $(TARGET_DIR)/usr/share/keyrings
	cp -dp $(@D)/usr/share/keyrings/* $(TARGET_DIR)/usr/share/keyrings/
endef

$(eval $(generic-package))
