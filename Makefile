# Cherry: build wrapper around Buildroot (this repository is a BR2_EXTERNAL tree).
#
#   make             configure (first time) and build output/<name>/images/
#   make qemu        UEFI HTTP boot of the image in QEMU + OVMF
#   make test        run the end-to-end smoke test in QEMU
#   make sdk         the cross toolchain alone, as a relocatable SDK tarball
#   make menuconfig | savedefconfig | linux-menuconfig | br-<target>

DEFCONFIG ?= cherry_x86_64_defconfig
O ?= $(CURDIR)/output/$(DEFCONFIG:_defconfig=)

export BR2_DL_DIR ?= $(CURDIR)/dl
export BR2_CCACHE_DIR ?= $(CURDIR)/.ccache

# DEFCONFIG also names the file Buildroot's savedefconfig writes, so pass its
# path: as a plain name it would land in buildroot/.
BR := $(MAKE) -C $(CURDIR)/buildroot O=$(abspath $(O)) BR2_EXTERNAL=$(CURDIR) \
	DEFCONFIG=$(CURDIR)/configs/$(DEFCONFIG)

.PHONY: all build menuconfig savedefconfig linux-menuconfig \
	linux-update-defconfig qemu test sdk clean help

all: build

buildroot/Makefile:
	git submodule update --init buildroot

$(O)/.config: | buildroot/Makefile
	$(BR) $(DEFCONFIG)

build: $(O)/.config
	$(BR)

# make <name>_defconfig configures output/<name> from configs/<name>_defconfig.
%_defconfig: | buildroot/Makefile
	$(MAKE) -C $(CURDIR)/buildroot O=$(CURDIR)/output/$* BR2_EXTERNAL=$(CURDIR) $@

menuconfig savedefconfig linux-menuconfig linux-update-defconfig: $(O)/.config
	$(BR) $@

# Any other Buildroot target, e.g. `make br-systemd-rebuild`.
br-%: $(O)/.config
	$(BR) $*

qemu:
	python3 scripts/run_qemu.py --images $(O)/images --state $(O)/qemu

# The toolchain of the main defconfig, built on its own from
# configs/cherry_sdk_x86_64_defconfig (keep their toolchain options the same),
# as Buildroot's relocatable SDK: output/cherry_sdk_x86_64/images/$(SDK).tar.gz,
# unpacked and fixed up with its relocate-sdk.sh. .github/workflows/sdk.yml
# publishes it as a GitHub release for a tag sdk-*.
SDK = cherry-sdk-x86_64
sdk:
	$(MAKE) DEFCONFIG=cherry_sdk_x86_64_defconfig BR2_SDK_PREFIX=$(SDK) br-sdk

test:
	python3 tests/smoke.py --images $(O)/images --state $(O)/test

clean:
	rm -rf $(O)

help:
	@sed -n '2,9s/^# \{0,1\}//p' Makefile
