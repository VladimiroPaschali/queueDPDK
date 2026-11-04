/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * Which Open-NIC bitstream is loaded on the U55N.
 *
 * Every variant keeps 0x0940907F in register 0x0 of the user BAR (the check
 * the apps already do), so they are told apart by two synthesis parameters of
 * the QDMA IP that the config BAR exposes, both in the PMD's EQDMA register
 * dump table:
 *
 *   0xBE0 C2H_PFCH_CACHE_DEPTH   bits 7:0  queues in the C2H prefetch cache
 *   0xBE4 C2H_WRB_COAL_BUF_DEPTH bits 7:0  completion coalescing buffer
 *
 * | bitstream                          | 0xBE0 | 0xBE4 |
 * |------------------------------------|-------|-------|
 * | extended_table.bit (original)      | 16    | 16    |
 * | extended_table_prefetch.bit (repo  | 64    | 16    |
 * |   root, built as onic_au55n_pfch64)|       |       |
 * | onic_au55n_coal32.bit              | 16    | 32    |
 * | onic_au55n_pfch64-coal32.bit       | 64    | 32    |
 *
 * The variants are built in ~/dpdk_patched/bitstream (README.md there). With 16
 * entries the NIC receive ceiling falls as the active queues share the prefetch
 * cache (one PCIe descriptor read per packet): the bitstream of the paper has 64.
 */
#ifndef QBITSTREAM_H
#define QBITSTREAM_H

#include <stdint.h>
#include <stdio.h>

/* QDMA PMD (qdma_platform.c): config BAR and user BAR reads */
uint32_t qdma_reg_read(void *dev_hndl, uint32_t reg_offst);
uint32_t qdma_reg_read_usr(void *dev_hndl, uint32_t reg_offst);

#define QBITSTREAM_PFCH_CACHE_DEPTH_ADDR   0xBE0
#define QBITSTREAM_WRB_COAL_BUF_DEPTH_ADDR 0xBE4

struct qbitstream {
	const char *name;
	uint32_t    pfch_cache_depth;
	uint32_t    wrb_coal_buf_depth;
};

/* dev is the struct rte_eth_dev * of the QDMA port */
static inline struct qbitstream
qbitstream_detect(void *dev)
{
	struct qbitstream b = {
		.name               = "unknown",
		.pfch_cache_depth   = qdma_reg_read(dev, QBITSTREAM_PFCH_CACHE_DEPTH_ADDR) & 0xff,
		.wrb_coal_buf_depth = qdma_reg_read(dev, QBITSTREAM_WRB_COAL_BUF_DEPTH_ADDR) & 0xff,
	};

	if (b.pfch_cache_depth == 16 && b.wrb_coal_buf_depth == 16)
		b.name = "extended_table";
	else if (b.pfch_cache_depth == 64 && b.wrb_coal_buf_depth == 16)
		b.name = "pfch64";
	else if (b.pfch_cache_depth == 16 && b.wrb_coal_buf_depth == 32)
		b.name = "coal32";
	else if (b.pfch_cache_depth == 64 && b.wrb_coal_buf_depth == 32)
		b.name = "pfch64-coal32";
	return b;
}

/* one line at startup, parsed by the experiment scripts: keep it stable */
static inline struct qbitstream
qbitstream_print(void *dev)
{
	const struct qbitstream b = qbitstream_detect(dev);

	printf("bitstream: %s (C2H prefetch cache %u queues, completion coalescing buffer %u)\n",
	       b.name, b.pfch_cache_depth, b.wrb_coal_buf_depth);
	return b;
}

#endif /* QBITSTREAM_H */
