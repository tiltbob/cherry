################################################################################
#
# ubuntu-keyring
#
################################################################################

# Debian's binary package of Ubuntu's keyring, from its snapshot archive, as
# debian-archive-keyring: building the keyrings from source needs gpg on the
# build host. Only the archive keyring is installed: the one debootstrap
# verifies an Ubuntu release against.
UBUNTU_KEYRING_VERSION = 2023.11.28.1-0.2
UBUNTU_KEYRING_SITE = https://snapshot.debian.org/archive/debian/20231130T032551Z/pool/main/u/ubuntu-keyring
UBUNTU_KEYRING_SOURCE = ubuntu-keyring_$(UBUNTU_KEYRING_VERSION)_all.deb
UBUNTU_KEYRING_LICENSE = GPL-2.0+
UBUNTU_KEYRING_LICENSE_FILES = usr/share/doc/ubuntu-keyring/copyright
UBUNTU_KEYRING_EXTRACT_DEPENDENCIES = $(BR2_XZCAT_HOST_DEPENDENCY)

define UBUNTU_KEYRING_EXTRACT_CMDS
	$(HOSTAR) p $(UBUNTU_KEYRING_DL_DIR)/$(UBUNTU_KEYRING_SOURCE) \
		data.tar.xz | $(XZCAT) | $(TAR) -C $(@D) $(TAR_OPTIONS) - ./usr/share
endef

define UBUNTU_KEYRING_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0644 $(@D)/usr/share/keyrings/ubuntu-archive-keyring.gpg \
		$(TARGET_DIR)/usr/share/keyrings/ubuntu-archive-keyring.gpg
endef

$(eval $(generic-package))
