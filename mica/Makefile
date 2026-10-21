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
	-pthread -lm -ldl -lnuma -lpcap -lcrypto -lz -lelf -lbsd -libverbs \
	$(shell pkg-config --libs jansson 2>/dev/null)  # librte_metrics telemetry, if DPDK was configured with jansson

# Variants of the port (see main.c): -fifo really uses FIFO eviction on a
# single core, -nocopy does not copy the 1400-byte value into the RX buffer
VARIANTS := $(addprefix build/$(APP)-,fifo nocopy fifo-nocopy)
build/$(APP)-fifo: CFLAGS += -DMICA_TRUE_FIFO
build/$(APP)-nocopy: CFLAGS += -DMICA_NO_VALUE_COPY
build/$(APP)-fifo-nocopy: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY
# Multicore on top of -fifo-nocopy: -pools gives every polling lcore its own
# log pool, -pools-padlock also puts each Shared queue lock on its own lines,
# -pools-affinity also keeps every Shared queue on one lcore unless it steals
VARIANTS += build/$(APP)-fifo-nocopy-pools build/$(APP)-fifo-nocopy-pools-padlock \
	build/$(APP)-fifo-nocopy-pools-affinity
build/$(APP)-fifo-nocopy-pools: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY \
	-DMICA_POOL_PER_LCORE -DMEHCACHED_MULTI_ALLOC
build/$(APP)-fifo-nocopy-pools-padlock: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY \
	-DMICA_POOL_PER_LCORE -DMEHCACHED_MULTI_ALLOC -DMICA_PADDED_QUEUE_LOCKS
build/$(APP)-fifo-nocopy-pools-affinity: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY \
	-DMICA_POOL_PER_LCORE -DMEHCACHED_MULTI_ALLOC -DMICA_PADDED_QUEUE_LOCKS -DMICA_SHARED_AFFINITY
# -fwd forwards without touching MICA: the ceiling of the NIC and the PMD
VARIANTS += build/$(APP)-fwd
build/$(APP)-fwd: CFLAGS += -DMICA_FORWARD_ONLY -DMICA_PADDED_QUEUE_LOCKS
# For latency.py and starvation.py: -lat writes nothing into the packet (the
# TRex latency trailer at the end of the frame comes back intact), -qprobe
# also logs every poll and the marked packets (../starvation/src/qprobe.h)
VARIANTS += build/$(APP)-fifo-nocopy-lat build/$(APP)-fifo-nocopy-qprobe
HDRS += ../starvation/src/qprobe.h
build/$(APP)-fifo-nocopy-lat: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY -DMICA_KEEP_PAYLOAD
build/$(APP)-fifo-nocopy-qprobe: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY -DMICA_KEEP_PAYLOAD \
	-DMICA_QPROBE -I../starvation/src
VARIANTS += build/$(APP)-fifo-nocopy-prefetch-lat build/$(APP)-fifo-nocopy-prefetch-qprobe
build/$(APP)-fifo-nocopy-prefetch-lat: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY -DMICA_KEEP_PAYLOAD \
	-DMICA_BATCH_PREFETCH
build/$(APP)-fifo-nocopy-prefetch-qprobe: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY -DMICA_KEEP_PAYLOAD \
	-DMICA_BATCH_PREFETCH -DMICA_QPROBE -I../starvation/src
# MICA's request pipeline (prefetch bucket, then item, then run the burst)
VARIANTS += build/$(APP)-fifo-nocopy-prefetch
build/$(APP)-fifo-nocopy-prefetch: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY -DMICA_BATCH_PREFETCH
# the multicore variants with the request pipeline (-pools-padlock-prefetch:
# the paper's Shared, every lcore polling every queue)
VARIANTS += build/$(APP)-fifo-nocopy-pools-prefetch build/$(APP)-fifo-nocopy-pools-affinity-prefetch \
	build/$(APP)-fifo-nocopy-pools-padlock-prefetch
build/$(APP)-fifo-nocopy-pools-padlock-prefetch: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY \
	-DMICA_POOL_PER_LCORE -DMEHCACHED_MULTI_ALLOC -DMICA_PADDED_QUEUE_LOCKS -DMICA_BATCH_PREFETCH
build/$(APP)-fifo-nocopy-pools-prefetch: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY \
	-DMICA_POOL_PER_LCORE -DMEHCACHED_MULTI_ALLOC -DMICA_BATCH_PREFETCH
build/$(APP)-fifo-nocopy-pools-affinity-prefetch: CFLAGS += -DMICA_TRUE_FIFO -DMICA_NO_VALUE_COPY \
	-DMICA_POOL_PER_LCORE -DMEHCACHED_MULTI_ALLOC -DMICA_PADDED_QUEUE_LOCKS -DMICA_SHARED_AFFINITY \
	-DMICA_BATCH_PREFETCH

all: build/$(APP) $(VARIANTS)

build/$(APP) $(VARIANTS): $(SRCS-y) $(HDRS) Makefile | build
	$(CC) $(CFLAGS) $(SRCS-y) -o $@ $(LDFLAGS)

build:
	@mkdir -p $@

.PHONY: all clean
clean:
	rm -rf build
