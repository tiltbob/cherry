################################################################################
#
# toml11
#
################################################################################

TOML11_VERSION = v4.4.0
TOML11_SITE = https://github.com/ToruNiina/toml11
TOML11_SITE_METHOD = git
TOML11_LICENSE = MIT
TOML11_LICENSE_FILES = LICENSE
TOML11_INSTALL_STAGING = YES
TOML11_INSTALL_TARGET = NO
TOML11_CONF_OPTS = -DTOML11_PRECOMPILE=OFF

$(eval $(cmake-package))
