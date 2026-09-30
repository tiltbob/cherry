################################################################################
#
# dnf5
#
################################################################################

DNF5_VERSION = 5.4.6.0
DNF5_SITE = https://github.com/rpm-software-management/dnf5
DNF5_SITE_METHOD = git
DNF5_LICENSE = GPL-2.0+, LGPL-2.1+ (libraries)
DNF5_LICENSE_FILES = COPYING.md gpl-2.0.txt lgpl-2.1.txt
DNF5_DEPENDENCIES = \
	host-pkgconf \
	fmt \
	json-c \
	libglib2 \
	librepo \
	libsolv-rpm \
	libxml2 \
	rpm6 \
	sqlite \
	toml11 \
	util-linux

# The CLI plugins need sdbus-c++ (automatic, needs-restarting) and the
# expired-pgp-keys plugin runs gpg; none of them is needed for
# --installroot.
DNF5_CONF_OPTS = \
	-DWITH_DNF5=ON \
	-DWITH_LIBDNF5_CLI=ON \
	-DWITH_DNF5_PLUGINS=OFF \
	-DWITH_DNF5DAEMON_CLIENT=OFF \
	-DWITH_DNF5DAEMON_SERVER=OFF \
	-DWITH_PLUGIN_ACTIONS=OFF \
	-DWITH_PLUGIN_APPSTREAM=OFF \
	-DWITH_PLUGIN_EXPIRED_PGP_KEYS=OFF \
	-DWITH_PLUGIN_LOCAL=OFF \
	-DWITH_PLUGIN_MANIFEST=OFF \
	-DWITH_PLUGIN_RHSM=OFF \
	-DWITH_PLUGIN_SYSTEMD_INHIBIT=OFF \
	-DWITH_PYTHON_PLUGINS_LOADER=OFF \
	-DWITH_COMPS=ON \
	-DWITH_MODULEMD=OFF \
	-DWITH_SYSTEMD=OFF \
	-DWITH_HTML=OFF \
	-DWITH_MAN=OFF \
	-DWITH_TESTS=OFF \
	-DWITH_PERFORMANCE_TESTS=OFF \
	-DWITH_SANITIZERS=OFF \
	-DWITH_GO=OFF \
	-DWITH_PERL5=OFF \
	-DWITH_PYTHON3=OFF \
	-DWITH_RUBY=OFF

ifeq ($(BR2_SYSTEM_ENABLE_NLS),y)
DNF5_CONF_OPTS += -DWITH_TRANSLATIONS=ON
DNF5_DEPENDENCIES += host-gettext $(TARGET_NLS_DEPENDENCIES)
else
DNF5_CONF_OPTS += -DWITH_TRANSLATIONS=OFF
endif

ifeq ($(BR2_PACKAGE_ACL),y)
DNF5_DEPENDENCIES += acl
DNF5_CONF_OPTS += -DWITH_ACL=ON
else
DNF5_CONF_OPTS += -DWITH_ACL=OFF
endif

# Upstream builds with -Werror; don't fail on a newer toolchain's warnings.
define DNF5_DISABLE_WERROR
	$(SED) 's/^add_compile_options(-Wall -Wextra -Werror)$$/add_compile_options(-Wall -Wextra)/' \
		$(@D)/CMakeLists.txt
endef
DNF5_POST_PATCH_HOOKS += DNF5_DISABLE_WERROR

# Fedora ships the aliases for the CLI plugins with them; without the
# plugins, dnf5 warns about each alias on every run.
define DNF5_INSTALL_TARGET_FIXUP
	ln -sf dnf5 $(TARGET_DIR)/usr/bin/dnf
	rm -f $(TARGET_DIR)/usr/share/dnf5/aliases.d/compatibility-plugins.conf
endef
DNF5_POST_INSTALL_TARGET_HOOKS += DNF5_INSTALL_TARGET_FIXUP

$(eval $(cmake-package))
