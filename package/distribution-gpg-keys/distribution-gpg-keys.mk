################################################################################
#
# distribution-gpg-keys
#
################################################################################

# Upstream tags are named after the Fedora package's NVR.
DISTRIBUTION_GPG_KEYS_VERSION = distribution-gpg-keys-1.123-1
DISTRIBUTION_GPG_KEYS_SITE = https://github.com/rpm-software-management/distribution-gpg-keys
DISTRIBUTION_GPG_KEYS_SITE_METHOD = git
DISTRIBUTION_GPG_KEYS_LICENSE = CC0-1.0
DISTRIBUTION_GPG_KEYS_LICENSE_FILES = LICENSE

# Like Fedora's main package: the copr keys (144 MB) are a subpackage there.
define DISTRIBUTION_GPG_KEYS_INSTALL_TARGET_CMDS
	mkdir -p $(TARGET_DIR)/usr/share/distribution-gpg-keys
	rsync -a --exclude=/copr $(@D)/keys/ \
		$(TARGET_DIR)/usr/share/distribution-gpg-keys/
endef

$(eval $(generic-package))
