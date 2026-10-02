################################################################################
#
# kubo
#
################################################################################

KUBO_VERSION = v0.43.1
KUBO_SITE = https://github.com/ipfs/kubo
KUBO_SITE_METHOD = git
KUBO_LICENSE = MIT (code from before 2019-05-06), Apache-2.0 or MIT
KUBO_LICENSE_FILES = LICENSE LICENSE-APACHE LICENSE-MIT
KUBO_BUILD_TARGETS = cmd/ipfs
# ipfs mount needs FUSE, which Cherry's kernel doesn't have.
KUBO_TAGS = nofuse
KUBO_USERS = ipfs -1 ipfs -1 * /var/lib/ipfs - - IPFS daemon

# The daemon's repository, for ipfs commands run from a login shell.
define KUBO_INSTALL_PROFILE
	$(INSTALL) -D -m 0644 $(KUBO_PKGDIR)/ipfs.sh $(TARGET_DIR)/etc/profile.d/ipfs.sh
endef
KUBO_POST_INSTALL_TARGET_HOOKS += KUBO_INSTALL_PROFILE

define KUBO_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(KUBO_PKGDIR)/ipfs.service \
		$(TARGET_DIR)/usr/lib/systemd/system/ipfs.service
	$(INSTALL) -D -m 0644 $(KUBO_PKGDIR)/50-ipfs.conf \
		$(TARGET_DIR)/usr/lib/sysctl.d/50-ipfs.conf
endef

$(eval $(golang-package))
