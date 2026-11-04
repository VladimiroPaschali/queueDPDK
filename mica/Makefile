# SPDX-License-Identifier: BSD-3-Clause
# Copyright(c) 2010-2014 Intel Corporation

APP = mica

SRCS-y := main.c
SRCS-y += $(wildcard ported-mica/*.c)
HDRS := $(wildcard ported-mica/*.h) ../common/qflow.h ../common/qbitstream.h

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
	-pthread -lm -ldl -lnuma -lpcap -lcrypto -lz -lelf -lbsd -libverbs \
	$(shell pkg-config --libs jansson 2>/dev/null)  # librte_metrics telemetry, if DPDK was configured with jansson

# Every build is the MICA of the paper: true FIFO eviction, no copy of the
# 1400-byte value into the RX buffer, and MICA's request pipeline (prefetch
# the bucket, then the item, then run the burst); see main.c.
# The names are those of the experiments (experiments/no-drop-zip.py).
#
# -fifo-nocopy-prefetch: single core (NDR, theoretical max)
# -fifo-nocopy-pools-prefetch: Partitioned on 2 or 4 cores; -pools gives every
#   polling lcore its own log pool (one lock on the single pool made 2 cores
#   slower than 1)
VARIANTS := build/$(APP)-fifo-nocopy-prefetch build/$(APP)-fifo-nocopy-pools-prefetch
build/$(APP)-fifo-nocopy-pools-prefetch: CFLAGS += -DMICA_POOL_PER_LCORE -DMEHCACHED_MULTI_ALLOC

# For latency.py, starvation.py and adaptive.py --app mica (single core): -lat
# writes nothing into the packet (the TRex latency trailer at the end of the
# frame comes back intact), -qprobe also logs every poll and the marked packets
# (../common/qprobe.h), -adaptive picks the number of active RX queues at
# runtime (../common/adaptive.h; qdma_rss.c needs the PMD's private headers)
QDMA_SRC := $(DPDK_LOCAL_PATH)/drivers/net/qdma
VARIANTS += build/$(APP)-fifo-nocopy-prefetch-lat build/$(APP)-fifo-nocopy-prefetch-qprobe \
	build/$(APP)-fifo-nocopy-prefetch-adaptive
HDRS += ../common/qprobe.h ../common/adaptive.h ../common/qdma_rss.h ../common/qdma_rss.c
build/$(APP)-fifo-nocopy-prefetch-lat: CFLAGS += -DMICA_KEEP_PAYLOAD
build/$(APP)-fifo-nocopy-prefetch-qprobe: CFLAGS += -DMICA_KEEP_PAYLOAD -DMICA_QPROBE
build/$(APP)-fifo-nocopy-prefetch-adaptive: SRCS-y += ../common/qdma_rss.c
build/$(APP)-fifo-nocopy-prefetch-adaptive: CFLAGS += -DMICA_KEEP_PAYLOAD -DMICA_ADAPTIVE \
	-I$(QDMA_SRC) -I$(QDMA_SRC)/qdma_access -I$(QDMA_SRC)/qdma_access/qdma_soft_access

all: $(VARIANTS)

$(VARIANTS): $(SRCS-y) $(HDRS) Makefile | build
	$(CC) $(CFLAGS) $(SRCS-y) -o $@ $(LDFLAGS)

build:
	@mkdir -p $@

.PHONY: all clean
clean:
	rm -rf build
