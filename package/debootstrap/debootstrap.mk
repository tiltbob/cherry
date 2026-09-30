################################################################################
#
# debootstrap
#
################################################################################

DEBOOTSTRAP_VERSION = 1.0.145
DEBOOTSTRAP_SITE = https://snapshot.debian.org/archive/debian/20260831T204404Z/pool/main/d/debootstrap
DEBOOTSTRAP_SOURCE = debootstrap_$(DEBOOTSTRAP_VERSION).tar.gz
DEBOOTSTRAP_LICENSE = MIT
DEBOOTSTRAP_LICENSE_FILES = debian/copyright

# Without dpkg on the host, debootstrap needs --arch unless it finds a
# default in /usr/share/debootstrap/arch.
ifeq ($(BR2_x86_64),y)
DEBOOTSTRAP_ARCH = amd64
else ifeq ($(BR2_i386),y)
DEBOOTSTRAP_ARCH = i386
else ifeq ($(BR2_aarch64),y)
DEBOOTSTRAP_ARCH = arm64
else ifeq ($(BR2_arm)$(BR2_ARM_EABIHF),yy)
DEBOOTSTRAP_ARCH = armhf
else ifeq ($(BR2_arm),y)
DEBOOTSTRAP_ARCH = armel
else ifeq ($(BR2_powerpc64le),y)
DEBOOTSTRAP_ARCH = ppc64el
else ifeq ($(BR2_riscv)$(BR2_RISCV_64),yy)
DEBOOTSTRAP_ARCH = riscv64
else ifeq ($(BR2_s390x),y)
DEBOOTSTRAP_ARCH = s390x
endif

define DEBOOTSTRAP_INSTALL_TARGET_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) DESTDIR=$(TARGET_DIR) install
	$(if $(DEBOOTSTRAP_ARCH),
		echo $(DEBOOTSTRAP_ARCH) > $(TARGET_DIR)/usr/share/debootstrap/arch)
endef

$(eval $(generic-package))
