################################################################################
#
# bun
#
################################################################################

BUN_VERSION = 1.4.2
BUN_SITE = https://github.com/oven-sh/bun/releases/download/bun-v$(BUN_VERSION)
# Upstream's prebuilt binary: building Bun needs Zig and LLVM. The "baseline"
# x86_64 build doesn't need AVX2, so it also runs on VMs whose CPU model lacks
# it (QEMU's qemu64, x86-64-v2). The zip holds the binary under one directory.
# Unlike opencode, the runtime itself survives Buildroot's strip.
BUN_SOURCE = bun-linux-x64-baseline.zip
# The release doesn't carry the license; take it from the tagged source.
BUN_EXTRA_DOWNLOADS = https://raw.githubusercontent.com/oven-sh/bun/bun-v$(BUN_VERSION)/LICENSE.md
BUN_LICENSE = MIT (Bun), LGPL-2.1+ (JavaScriptCore), MIT, BSD and Apache family licenses (other bundled components)
BUN_LICENSE_FILES = LICENSE.md

define BUN_EXTRACT_CMDS
	$(UNZIP) -d $(@D) $(BUN_DL_DIR)/$(BUN_SOURCE)
	mv $(@D)/bun-linux-x64-baseline/bun $(@D)/bun
	rmdir $(@D)/bun-linux-x64-baseline
	cp $(BUN_DL_DIR)/LICENSE.md $(@D)/LICENSE.md
endef

define BUN_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/bun $(TARGET_DIR)/usr/bin/bun
endef

$(eval $(generic-package))
