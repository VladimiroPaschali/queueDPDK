# SPDX-License-Identifier: BSD-3-Clause
# Copyright(c) 2010-2014 Intel Corporation

# binary name
APP = asni-nat

# all source are stored in SRCS-y
# SRCS-y := main.c
SRCS-y := $(wildcard src/*.c)
SRCS-y += $(wildcard src/utils/*.c)




# Build using pkg-config variables if possible
ifneq ($(shell pkg-config --exists libdpdk && echo 0),0)
$(error "no installation of DPDK found")
endif

#make local for local build using DPDK-25.11 source tree
all: local
.PHONY: shared static local
shared: build/$(APP)-shared
	ln -sf $(APP)-shared build/$(APP)
static: build/$(APP)-static
	ln -sf $(APP)-static build/$(APP)
local: build/$(APP)-local
	ln -sf $(APP)-local build/$(APP)

PKGCONF ?= pkg-config

PC_FILE := $(shell $(PKGCONF) --path libdpdk 2>/dev/null)
CFLAGS += -O3 $(shell $(PKGCONF) --cflags libdpdk)
# Add flag to allow experimental API as cms uses rte_ethdev_set_ptype API
CFLAGS += -DALLOW_EXPERIMENTAL_API
LDFLAGS_SHARED = $(shell $(PKGCONF) --libs libdpdk)
LDFLAGS_STATIC = $(shell $(PKGCONF) --static --libs libdpdk)

build/$(APP)-shared: $(SRCS-y) Makefile $(PC_FILE) | build
	$(CC) $(CFLAGS) $(SRCS-y) -o $@ $(LDFLAGS) $(LDFLAGS_SHARED)

build/$(APP)-static: $(SRCS-y) Makefile $(PC_FILE) | build
	$(CC) $(CFLAGS) $(SRCS-y) -o $@ $(LDFLAGS) $(LDFLAGS_STATIC)


# Local build using DPDK-25.11 source tree (Meson build)
DPDK_LOCAL_PATH ?= /home/vladimiro/ucl/dpdk-25.11
DPDK_BUILD ?= $(DPDK_LOCAL_PATH)/build
PKG_CONFIG_LOCAL := PKG_CONFIG_PATH=$(DPDK_BUILD)/meson-uninstalled

CFLAGS_LOCAL += $(shell $(PKG_CONFIG_LOCAL) pkg-config --cflags libdpdk) -DALLOW_EXPERIMENTAL_API -O3 -march=native
LDFLAGS_LOCAL = $(shell $(PKG_CONFIG_LOCAL) pkg-config --static --libs libdpdk)


build/$(APP)-local: $(SRCS-y) Makefile | build
	$(CC) $(CFLAGS_LOCAL) $(SRCS-y) -o $@ $(LDFLAGS_LOCAL)

build:
	@mkdir -p $@

.PHONY: clean
clean:
	rm -rdf build
	test -d build && rmdir -p build || true