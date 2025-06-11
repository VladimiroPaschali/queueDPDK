# SPDX-License-Identifier: BSD-3-Clause
# Copyright(c) 2010-2014 Intel Corporation

# ====== CONFIGURAZIONE ======
APP = cms
SRCS = main.c
BUILD_DIR = build

# Path alla build statica DPDK (modifica se necessario)
DPDK_BUILD = ../../dpdk-20.11/build

# Compilatore
CC = gcc

# ====== SHARED MODE (pkg-config) ======
PKGCONF ?= pkg-config
PC_FILE := $(shell $(PKGCONF) --path libdpdk 2>/dev/null)

CFLAGS_SHARED = -O3 -g -DALLOW_EXPERIMENTAL_API \
	$(shell $(PKGCONF) --cflags libdpdk) \
	-DRTE_ETHDEV_PROFILE_WITH_VTUNE -DRTE_ETHDEV_RXTX_CALLBACKS

LDFLAGS_SHARED = $(shell $(PKGCONF) --libs libdpdk)

# ====== STATIC MODE (no install) ======
CFLAGS_STATIC = -O3 -g -mssse3 -DALLOW_EXPERIMENTAL_API \
	-I$(DPDK_BUILD)/include \
	-DRTE_ETHDEV_PROFILE_WITH_VTUNE -DRTE_ETHDEV_RXTX_CALLBACKS

DPDK_LIBS_STATIC := $(wildcard $(DPDK_BUILD)/lib/*.a $(DPDK_BUILD)/drivers/*.a)

LDFLAGS_STATIC = -Wl,--start-group $(DPDK_LIBS_STATIC) -Wl,--end-group \
	-ldl -lm -pthread -lnuma

# ====== REGOLE ======

all: static

.PHONY: shared static clean all

shared: $(BUILD_DIR)/$(APP)-shared
	ln -sf $(APP)-shared $(BUILD_DIR)/$(APP)

static: $(BUILD_DIR)/$(APP)-static
	ln -sf $(APP)-static $(BUILD_DIR)/$(APP)

# Compilazione shared (con pkg-config)
$(BUILD_DIR)/$(APP)-shared: $(SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS_SHARED) $^ -o $@ $(LDFLAGS_SHARED)

# Compilazione static (con librerie da build locale)
$(BUILD_DIR)/$(APP)-static: $(SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS_STATIC) $^ -o $@ $(LDFLAGS_STATIC)

# Crea cartella build
$(BUILD_DIR):
	@mkdir -p $@

# Pulizia
clean:
	rm -f $(BUILD_DIR)/$(APP) $(BUILD_DIR)/$(APP)-static $(BUILD_DIR)/$(APP)-shared
	test -d $(BUILD_DIR) && rmdir $(BUILD_DIR) || true
