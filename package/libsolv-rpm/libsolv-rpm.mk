################################################################################
#
# libsolv-rpm
#
################################################################################

LIBSOLV_RPM_VERSION = 0.7.40
LIBSOLV_RPM_SITE = https://github.com/openSUSE/libsolv
LIBSOLV_RPM_SITE_METHOD = git
LIBSOLV_RPM_LICENSE = BSD-3-Clause
LIBSOLV_RPM_LICENSE_FILES = LICENSE.BSD
LIBSOLV_RPM_CPE_ID_VENDOR = opensuse
LIBSOLV_RPM_CPE_ID_PRODUCT = libsolv
LIBSOLV_RPM_INSTALL_STAGING = YES
LIBSOLV_RPM_DEPENDENCIES = host-pkgconf bzip2 libxml2 rpm6 xz zchunk zlib zstd

# Fedora's configuration (libsolv.spec), without the language bindings.
LIBSOLV_RPM_CONF_OPTS = \
	-DFEDORA=1 \
	-DENABLE_RPMDB=ON \
	-DENABLE_RPMDB_BYRPMHEADER=ON \
	-DENABLE_RPMDB_LIBRPM=ON \
	-DENABLE_RPMPKG_LIBRPM=ON \
	-DENABLE_RPMMD=ON \
	-DENABLE_COMPS=ON \
	-DENABLE_COMPLEX_DEPS=ON \
	-DWITH_LIBXML2=ON \
	-DENABLE_LZMA_COMPRESSION=ON \
	-DENABLE_BZIP2_COMPRESSION=ON \
	-DENABLE_ZSTD_COMPRESSION=ON \
	-DENABLE_ZCHUNK_COMPRESSION=ON \
	-DWITH_SYSTEM_ZCHUNK=ON \
	-DENABLE_STATIC=OFF \
	-DDISABLE_SHARED=OFF

$(eval $(cmake-package))
