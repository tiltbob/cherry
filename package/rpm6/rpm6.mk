################################################################################
#
# rpm6
#
################################################################################

RPM6_VERSION_MAJOR = 6.1
RPM6_VERSION = $(RPM6_VERSION_MAJOR).0
RPM6_SOURCE = rpm-$(RPM6_VERSION).tar.bz2
RPM6_SITE = http://ftp.rpm.org/releases/rpm-$(RPM6_VERSION_MAJOR).x
RPM6_LICENSE = GPL-2.0+ or LGPL-2.0+ (library only)
RPM6_LICENSE_FILES = COPYING
RPM6_CPE_ID_VENDOR = rpm
RPM6_CPE_ID_PRODUCT = rpm
RPM6_INSTALL_STAGING = YES
RPM6_DEPENDENCIES = \
	host-pkgconf \
	bzip2 \
	file \
	libarchive \
	lua \
	openssl \
	popt \
	rpm-sequoia \
	sqlite \
	xz \
	zlib \
	zstd \
	$(TARGET_NLS_DEPENDENCIES)

# scdoc only renders the man pages, which the target doesn't keep (an
# absolute path: CMake makes a relative one relative to the build directory).
# libelf/libdw serve rpmbuild's debuginfo and ELF dependency generators.
RPM6_CONF_OPTS = \
	-DENABLE_OPENMP=OFF \
	-DENABLE_PYTHON=OFF \
	-DENABLE_TESTSUITE=OFF \
	-DENABLE_SQLITE=ON \
	-DENABLE_NDB=ON \
	-DENABLE_BDB_RO=OFF \
	-DWITH_SEQUOIA=ON \
	-DWITH_OPENSSL=ON \
	-DWITH_BZIP2=ON \
	-DWITH_ICONV=ON \
	-DWITH_LIBLZMA=ON \
	-DWITH_ZSTD=ON \
	-DWITH_LIBELF=OFF \
	-DWITH_LIBDW=OFF \
	-DWITH_DBUS=OFF \
	-DWITH_FAPOLICYD=OFF \
	-DWITH_FSVERITY=OFF \
	-DWITH_IMAEVM=OFF \
	-DWITH_DOXYGEN=OFF \
	-DSCDOC=/bin/true

ifeq ($(BR2_SYSTEM_ENABLE_NLS),y)
RPM6_CONF_OPTS += -DENABLE_NLS=ON
else
RPM6_CONF_OPTS += -DENABLE_NLS=OFF
endif

ifeq ($(BR2_PACKAGE_ACL),y)
RPM6_DEPENDENCIES += acl
RPM6_CONF_OPTS += -DWITH_ACL=ON
else
RPM6_CONF_OPTS += -DWITH_ACL=OFF
endif

ifeq ($(BR2_PACKAGE_AUDIT),y)
RPM6_DEPENDENCIES += audit
RPM6_CONF_OPTS += -DWITH_AUDIT=ON
else
RPM6_CONF_OPTS += -DWITH_AUDIT=OFF
endif

ifeq ($(BR2_PACKAGE_LIBCAP),y)
RPM6_DEPENDENCIES += libcap
RPM6_CONF_OPTS += -DWITH_CAP=ON
else
RPM6_CONF_OPTS += -DWITH_CAP=OFF
endif

ifeq ($(BR2_PACKAGE_LIBSELINUX),y)
RPM6_DEPENDENCIES += libselinux
RPM6_CONF_OPTS += -DWITH_SELINUX=ON
else
RPM6_CONF_OPTS += -DWITH_SELINUX=OFF
endif

ifeq ($(BR2_PACKAGE_READLINE),y)
RPM6_DEPENDENCIES += readline
RPM6_CONF_OPTS += -DWITH_READLINE=ON
else
RPM6_CONF_OPTS += -DWITH_READLINE=OFF
endif

$(eval $(cmake-package))
