# Cherry: build wrapper around Buildroot (this repository is a BR2_EXTERNAL tree).
#
#   make keys        generate a development RAUC signing key (once)
#   make             configure (first time) and build output/<name>/images/
#   make qemu        boot the image in QEMU + OVMF
#   make test        run the end-to-end smoke test in QEMU
#   make menuconfig | savedefconfig | linux-menuconfig | br-<target>

DEFCONFIG ?= cherry_x86_64_defconfig
O ?= $(CURDIR)/output/$(DEFCONFIG:_defconfig=)

export BR2_DL_DIR ?= $(CURDIR)/dl
export BR2_CCACHE_DIR ?= $(CURDIR)/.ccache

CHERRY_RAUC_KEY ?= $(CURDIR)/keys/dev.key.pem
CHERRY_RAUC_CERT ?= $(CURDIR)/keys/dev.cert.pem
CHERRY_RAUC_KEYRING ?= $(CHERRY_RAUC_CERT)

# Buildroot runs the post-build/post-image scripts from its own directory, so
# pass absolute paths (PKCS#11 URIs are left alone).
cherry_abspath = $(if $(filter pkcs11:%,$(1)),$(1),$(abspath $(1)))
override CHERRY_RAUC_KEY := $(call cherry_abspath,$(CHERRY_RAUC_KEY))
override CHERRY_RAUC_CERT := $(call cherry_abspath,$(CHERRY_RAUC_CERT))
override CHERRY_RAUC_KEYRING := $(abspath $(CHERRY_RAUC_KEYRING))
export CHERRY_RAUC_KEY CHERRY_RAUC_CERT CHERRY_RAUC_KEYRING

BR := $(MAKE) -C $(CURDIR)/buildroot O=$(abspath $(O)) BR2_EXTERNAL=$(CURDIR)

.PHONY: all build check-keys keys menuconfig savedefconfig linux-menuconfig \
	linux-update-defconfig qemu qemu-reset test clean help

all: build

buildroot/Makefile:
	git submodule update --init buildroot

$(O)/.config: | buildroot/Makefile
	$(BR) $(DEFCONFIG)

check-keys:
	@for f in "$(CHERRY_RAUC_KEY)" "$(CHERRY_RAUC_CERT)" "$(CHERRY_RAUC_KEYRING)"; do \
		case "$$f" in pkcs11:*) continue ;; esac; \
		[ -r "$$f" ] || { echo "error: $$f not found; run 'make keys' or set CHERRY_RAUC_KEY/CERT/KEYRING" >&2; exit 1; }; \
	done

build: check-keys $(O)/.config
	$(BR)

keys:
	scripts/gen-keys.sh $(CURDIR)/keys

%_defconfig: | buildroot/Makefile
	$(BR) $@

menuconfig savedefconfig linux-menuconfig linux-update-defconfig: $(O)/.config
	$(BR) $@

# Any other Buildroot target, e.g. `make br-rauc-rebuild`.
br-%: $(O)/.config
	$(BR) $*

qemu:
	python3 scripts/run_qemu.py --images $(O)/images --state $(O)/qemu

qemu-reset:
	rm -rf $(O)/qemu

test:
	python3 tests/smoke.py --images $(O)/images --state $(O)/test

clean:
	rm -rf $(O)

help:
	@sed -n '2,9s/^# \{0,1\}//p' Makefile
