# Cherry: build wrapper around Buildroot (this repository is a BR2_EXTERNAL tree).
#
#   make             configure (first time) and build output/<name>/images/
#   make qemu        UEFI HTTP boot of the image in QEMU + OVMF
#   make test        run the end-to-end smoke test in QEMU
#   make sdk         the cross toolchain alone, as a relocatable SDK tarball
#   make check       fast checks that need no build: lint, units, scripts, tests
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
	linux-update-defconfig qemu test sdk check $(CHECKS) clean help

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

# Fast checks that need no build, each failing loudly: `make check` runs them
# all (`make -k check` goes on after a failure), `make check-<name>` one. CI
# runs them before the build (.github/workflows/build.yml). They need
# shellcheck, flake8, python3-magic and python3-yaml, systemd-analyze, and
# node for the polkit rules (skipped without it).
CHECKS = check-package check-flake8 check-yaml check-syntax check-units \
	check-scenarios check-polkit check-bootstrap check-postbuild

check: $(CHECKS)
	@echo "make check: $(words $(CHECKS)) checks passed"

# Buildroot's lint for Config.in, every file under package/ and the defconfigs
# (package .mk, .hash and Config.in rules, patches, shellcheck on scripts, the
# whitespace rules; .shellcheckrc applies).
check-package: | buildroot/Makefile
	buildroot/utils/check-package -b Config.in $$(find package configs -type f | sort)

check-flake8:
	python3 -m flake8 --max-line-length=120 tests/*.py scripts/*.py
	@echo "flake8: tests/*.py scripts/*.py are clean"

check-yaml:
	python3 -c 'import sys, yaml; [yaml.safe_load(open(f)) for f in sys.argv[1:]]; print("yaml:", *sys.argv[1:], "parse")' \
		.github/workflows/*.yml

# Every shell script under package/ and board/ parses (sh -n, or bash -n).
check-syntax:
	tests/syntax.sh

# The systemd units verify against a fake root laid out as the image.
check-units:
	tests/units.sh

# The smoke test runs every s<N>_ scenario, in order, with no gap.
check-scenarios:
	python3 tests/scenarios.py

# The polkit rules allow the cherry user what the README says, nothing more.
check-polkit:
	@if command -v node >/dev/null 2>&1; then node tests/polkit.js; \
	else echo "polkit: SKIPPED, node is not installed (package/cherry/50-cherry.rules unchecked)"; fi

# cherry-bootstrap checks its instance and calls the tools right.
check-bootstrap:
	tests/bootstrap.sh

# post-build.sh still catches a stripped opencode.
check-postbuild:
	tests/postbuild.sh

clean:
	rm -rf $(O)

help:
	@sed -n '2,10s/^# \{0,1\}//p' Makefile
