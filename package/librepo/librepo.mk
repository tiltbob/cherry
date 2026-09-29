################################################################################
#
# librepo
#
################################################################################

LIBREPO_VERSION = 1.21.1
LIBREPO_SITE = https://github.com/rpm-software-management/librepo
LIBREPO_SITE_METHOD = git
LIBREPO_LICENSE = LGPL-2.1+
LIBREPO_LICENSE_FILES = COPYING
LIBREPO_INSTALL_STAGING = YES
LIBREPO_DEPENDENCIES = host-pkgconf libcurl libglib2 libxml2 openssl rpm6 zchunk

# USE_GPGME=OFF: keys and signatures go through rpm (rpm-sequoia).
LIBREPO_CONF_OPTS = \
	-DENABLE_TESTS=OFF \
	-DENABLE_DOCS=OFF \
	-DENABLE_EXAMPLES=OFF \
	-DENABLE_PYTHON=OFF \
	-DENABLE_SELINUX=OFF \
	-DUSE_GPGME=OFF \
	-DWITH_ZCHUNK=ON

$(eval $(cmake-package))
