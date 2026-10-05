# SPDX-License-Identifier: BSD-3-Clause
# Copyright(c) 2010-2014 Intel Corporation

# binary names
APP = app
APP_MULTI_RX_SINGLE_TX = app_multi_rx_single_tx
APP_MULTI_RX_MULTI_TX = app_multi_rx_multi_tx

# source files
SRCS_APP := main.c
SRCS_MULTI_RX_SINGLE_TX := app_multi_rx_single_tx.c
SRCS_MULTI_RX_MULTI_TX := app_multi_rx_multi_tx.c

# Build using pkg-config variables if possible
ifneq ($(shell pkg-config --exists libdpdk && echo 0),0)
$(error "no installation of DPDK found")
endif

all: local
.PHONY: shared static local clean
shared: build/$(APP)-shared build/$(APP_MULTI_RX_SINGLE_TX)-shared build/$(APP_MULTI_RX_MULTI_TX)-shared
	ln -sf $(APP)-shared build/$(APP)

static: build/$(APP)-static build/$(APP_MULTI_RX_SINGLE_TX)-static build/$(APP_MULTI_RX_MULTI_TX)-static
	ln -sf $(APP)-static build/$(APP)

local: build/$(APP)-local build/$(APP_MULTI_RX_SINGLE_TX)-local build/$(APP_MULTI_RX_MULTI_TX)-local
	ln -sf $(APP)-local build/$(APP)

PKGCONF ?= pkg-config

PC_FILE := $(shell $(PKGCONF) --path libdpdk 2>/dev/null)
CFLAGS += -O3 $(shell $(PKGCONF) --cflags libdpdk)
CFLAGS += -DALLOW_EXPERIMENTAL_API
LDFLAGS_SHARED = $(shell $(PKGCONF) --libs libdpdk)
LDFLAGS_STATIC = $(shell $(PKGCONF) --static --libs libdpdk)

build/$(APP)-shared: $(SRCS_APP) Makefile $(PC_FILE) | build
	$(CC) $(CFLAGS) $(SRCS_APP) -o $@ $(LDFLAGS) $(LDFLAGS_SHARED)

build/$(APP)-static: $(SRCS_APP) Makefile $(PC_FILE) | build
	$(CC) $(CFLAGS) $(SRCS_APP) -o $@ $(LDFLAGS) $(LDFLAGS_STATIC)

build/$(APP_MULTI_RX_SINGLE_TX)-shared: $(SRCS_MULTI_RX_SINGLE_TX) Makefile $(PC_FILE) | build
	$(CC) $(CFLAGS) $(SRCS_MULTI_RX_SINGLE_TX) -o $@ $(LDFLAGS) $(LDFLAGS_SHARED)

build/$(APP_MULTI_RX_SINGLE_TX)-static: $(SRCS_MULTI_RX_SINGLE_TX) Makefile $(PC_FILE) | build
	$(CC) $(CFLAGS) $(SRCS_MULTI_RX_SINGLE_TX) -o $@ $(LDFLAGS) $(LDFLAGS_STATIC)

build/$(APP_MULTI_RX_MULTI_TX)-shared: $(SRCS_MULTI_RX_MULTI_TX) Makefile $(PC_FILE) | build
	$(CC) $(CFLAGS) $(SRCS_MULTI_RX_MULTI_TX) -o $@ $(LDFLAGS) $(LDFLAGS_SHARED)

build/$(APP_MULTI_RX_MULTI_TX)-static: $(SRCS_MULTI_RX_MULTI_TX) Makefile $(PC_FILE) | build
	$(CC) $(CFLAGS) $(SRCS_MULTI_RX_MULTI_TX) -o $@ $(LDFLAGS) $(LDFLAGS_STATIC)

# Local build using DPDK-20.11 source tree
DPDK_LOCAL_PATH ?= ../../dpdk-20.11
DPDK_BUILD ?= $(DPDK_LOCAL_PATH)/build

DPDK_INCLUDES := -I$(DPDK_LOCAL_PATH) -I$(DPDK_BUILD)/include
DPDK_LIBS := $(wildcard $(DPDK_BUILD)/lib/librte_*.a)
EXCLUDED_DRIVERS := librte_net_mlx4.a librte_common_mlx5.a librte_vdpa_mlx5.a librte_regex_mlx5.a librte_net_mlx5.a
DPDK_DRIVERS := $(filter-out $(addprefix $(DPDK_BUILD)/drivers/, $(EXCLUDED_DRIVERS)), $(wildcard $(DPDK_BUILD)/drivers/librte_*.a))

CFLAGS_LOCAL += $(DPDK_INCLUDES) -DALLOW_EXPERIMENTAL_API -O3 -march=native
LDFLAGS_LOCAL = -Wl,--as-needed -Wl,--no-undefined -Wl,-O1 \
	-Wl,--whole-archive $(DPDK_LIBS) $(DPDK_DRIVERS) -Wl,--no-whole-archive \
	-pthread -lm -ldl -lnuma -lpcap -lcrypto -lz -lelf -lbsd -libverbs

build/$(APP)-local: $(SRCS_APP) Makefile | build
	$(CC) $(CFLAGS_LOCAL) $(SRCS_APP) -o $@ $(LDFLAGS_LOCAL)

build/$(APP_MULTI_RX_SINGLE_TX)-local: $(SRCS_MULTI_RX_SINGLE_TX) Makefile | build
	$(CC) $(CFLAGS_LOCAL) $(SRCS_MULTI_RX_SINGLE_TX) -o $@ $(LDFLAGS_LOCAL)

build/$(APP_MULTI_RX_MULTI_TX)-local: $(SRCS_MULTI_RX_MULTI_TX) Makefile | build
	$(CC) $(CFLAGS_LOCAL) $(SRCS_MULTI_RX_MULTI_TX) -o $@ $(LDFLAGS_LOCAL)

build:
	@mkdir -p $@

.PHONY: clean
clean:
	rm -rdf build
	test -d build && rmdir -p build || true
