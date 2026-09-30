################################################################################
#
# rpm-sequoia
#
################################################################################

RPM_SEQUOIA_VERSION = v1.10.3
RPM_SEQUOIA_SITE = https://github.com/rpm-software-management/rpm-sequoia
RPM_SEQUOIA_SITE_METHOD = git
RPM_SEQUOIA_LICENSE = LGPL-2.0+
RPM_SEQUOIA_LICENSE_FILES = LICENSE.txt
RPM_SEQUOIA_INSTALL_STAGING = YES
RPM_SEQUOIA_DEPENDENCIES = host-pkgconf openssl

# The default backend is nettle; the image already has openssl.
RPM_SEQUOIA_CARGO_BUILD_OPTS = --no-default-features --features crypto-openssl
# build.rs writes these paths into rpm-sequoia.pc.
RPM_SEQUOIA_CARGO_ENV = PREFIX=/usr LIBDIR=/usr/lib

ifeq ($(BR2_ENABLE_DEBUG),y)
RPM_SEQUOIA_PROFILE = debug
else
RPM_SEQUOIA_PROFILE = release
endif

# A cdylib with no binaries, so the cargo infra's "cargo install --bins"
# doesn't apply. build.rs sets the soname to librpm_sequoia.so.1 and
# writes rpm-sequoia.pc next to the profile directory, not the target one.
define RPM_SEQUOIA_INSTALL_LIB
	$(INSTALL) -D -m 0755 \
		$(@D)/target/$(RUSTC_TARGET_NAME)/$(RPM_SEQUOIA_PROFILE)/librpm_sequoia.so \
		$(1)/usr/lib/librpm_sequoia.so.1
	ln -sf librpm_sequoia.so.1 $(1)/usr/lib/librpm_sequoia.so
endef

define RPM_SEQUOIA_INSTALL_STAGING_CMDS
	$(call RPM_SEQUOIA_INSTALL_LIB,$(STAGING_DIR))
	$(INSTALL) -D -m 0644 $(@D)/target/$(RPM_SEQUOIA_PROFILE)/rpm-sequoia.pc \
		$(STAGING_DIR)/usr/lib/pkgconfig/rpm-sequoia.pc
endef

define RPM_SEQUOIA_INSTALL_TARGET_CMDS
	$(call RPM_SEQUOIA_INSTALL_LIB,$(TARGET_DIR))
endef

$(eval $(cargo-package))
