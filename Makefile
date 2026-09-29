# Cherry: build wrapper around Buildroot (this repository is a BR2_EXTERNAL tree).
#
#   make             configure (first time) and build output/<name>/images/
#   make qemu        UEFI HTTP boot of the image in QEMU + OVMF
#   make test        run the end-to-end smoke test in QEMU
#   make menuconfig | savedefconfig | linux-menuconfig | br-<target>

DEFCONFIG ?= cherry_x86_64_defconfig
O ?= $(CURDIR)/output/$(DEFCONFIG:_defconfig=)

export BR2_DL_DIR ?= $(CURDIR)/dl
export BR2_CCACHE_DIR ?= $(CURDIR)/.ccache

BR := $(MAKE) -C $(CURDIR)/buildroot O=$(abspath $(O)) BR2_EXTERNAL=$(CURDIR)

.PHONY: all build menuconfig savedefconfig linux-menuconfig \
	linux-update-defconfig qemu test clean help

all: build

buildroot/Makefile:
	git submodule update --init buildroot

$(O)/.config: | buildroot/Makefile
	$(BR) $(DEFCONFIG)

build: $(O)/.config
	$(BR)

%_defconfig: | buildroot/Makefile
	$(BR) $@

menuconfig savedefconfig linux-menuconfig linux-update-defconfig: $(O)/.config
	$(BR) $@

# Any other Buildroot target, e.g. `make br-systemd-rebuild`.
br-%: $(O)/.config
	$(BR) $*

qemu:
	python3 scripts/run_qemu.py --images $(O)/images --state $(O)/qemu

test:
	python3 tests/smoke.py --images $(O)/images --state $(O)/test

clean:
	rm -rf $(O)

help:
	@sed -n '2,8s/^# \{0,1\}//p' Makefile
