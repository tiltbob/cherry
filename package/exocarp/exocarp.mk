################################################################################
#
# exocarp
#
################################################################################

# Cherry's own: the SSH host key agent, which keeps sshd's host key in the TPM
# (src/exocarp.c), its socket and service units, and the drop-in that makes
# sshd use it. The exocarp user runs it; the tss group, tpm2-tss's, gives it
# /dev/tpmrm0 (tpm2-tss's tpm-udev.rules).
EXOCARP_SITE = $(EXOCARP_PKGDIR)/src
EXOCARP_SITE_METHOD = local
EXOCARP_DEPENDENCIES = host-pkgconf openssl tpm2-tss
EXOCARP_USERS = exocarp -1 exocarp -1 * - - tss Cherry SSH host key agent

define EXOCARP_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -o $(@D)/exocarp $(@D)/exocarp.c \
		`$(PKG_CONFIG_HOST_BINARY) --cflags --libs tss2-esys tss2-tctildr tss2-rc libcrypto`
endef

define EXOCARP_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/exocarp $(TARGET_DIR)/usr/libexec/exocarp
endef

define EXOCARP_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(EXOCARP_PKGDIR)/exocarp.socket \
		$(TARGET_DIR)/usr/lib/systemd/system/exocarp.socket
	$(INSTALL) -D -m 0644 $(EXOCARP_PKGDIR)/exocarp.service \
		$(TARGET_DIR)/usr/lib/systemd/system/exocarp.service
	$(INSTALL) -D -m 0644 $(EXOCARP_PKGDIR)/sshd.conf \
		$(TARGET_DIR)/usr/lib/systemd/system/sshd.service.d/10-exocarp.conf
endef

$(eval $(generic-package))
