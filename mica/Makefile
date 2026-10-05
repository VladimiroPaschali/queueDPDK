# SPDX-License-Identifier: BSD-3-Clause
# Copyright(c) 2010-2014 Intel Corporation

APP = mica

SRCS-y := main.c
SRCS-y += $(wildcard ported-mica/*.c)
HDRS := $(wildcard ported-mica/*.h) ../common/qflow.h

# Static build against the patched DPDK 20.11 tree (QDMA PMD), which the
# application calls directly.
DPDK_LOCAL_PATH ?= ../../dpdk-20.11
DPDK_BUILD ?= $(DPDK_LOCAL_PATH)/build

DPDK_INCLUDES := -I$(DPDK_LOCAL_PATH) -I$(DPDK_BUILD)/include
DPDK_LIBS := $(wildcard $(DPDK_BUILD)/lib/librte_*.a)
# Drivers that are not needed and break the static link
EXCLUDED_DRIVERS := librte_net_mlx4.a librte_common_mlx5.a librte_vdpa_mlx5.a librte_regex_mlx5.a librte_net_mlx5.a
DPDK_DRIVERS := $(filter-out $(addprefix $(DPDK_BUILD)/drivers/, $(EXCLUDED_DRIVERS)), $(wildcard $(DPDK_BUILD)/drivers/librte_*.a))

CFLAGS += $(DPDK_INCLUDES) -I../common -DALLOW_EXPERIMENTAL_API -O3 -march=native
LDFLAGS += -Wl,--as-needed -Wl,--no-undefined -Wl,-O1 \
	-Wl,--whole-archive $(DPDK_LIBS) $(DPDK_DRIVERS) -Wl,--no-whole-archive \
	-pthread -lm -ldl -lnuma -lpcap -lcrypto -lz -lelf -lbsd -libverbs

all: build/$(APP)

build/$(APP): $(SRCS-y) $(HDRS) Makefile | build
	$(CC) $(CFLAGS) $(SRCS-y) -o $@ $(LDFLAGS)

build:
	@mkdir -p $@

.PHONY: all clean
clean:
	rm -rf build
