/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * Starvation and flow completion time probes.
 *
 * experiments/starvation-profile.py marks some packets in the UDP payload
 * (offset 42, right after the Ethernet/IPv4/UDP headers):
 *
 *   'Q' 'B'  flow_id(be32)  seq(be32)  shift(u8)    sampled Zipf flow; logged
 *                                                   when seq % 2^shift == 0
 *   'Q' 'L'  flow_id(be32)  ... TRex trailer        TRex latency stream (probe
 *                                                   or finite flow), always
 *                                                   logged
 *
 * TRex latency streams end with a 16-byte trailer:
 *   magic 0xAB (u8), flow_seq (u8), hw_id (u16), seq (u32), tx timestamp (u64)
 * where the timestamp is in TRex host ticks.
 *
 * Every poll reads the TSC once. Marked packets are appended to a per-lcore
 * log, dumped to a binary file on exit and analysed by experiments/starvation.py.
 *
 * Every poll also updates two per-queue gaps, measured on the polls that
 * return packets:
 *  - drain gap: now - t_drain[q], where t_drain[q] is the last poll that left
 *    q empty (nb_rx < MAX_PKT_BURST). It bounds the time the oldest packet of
 *    the burst spent in the RX ring; on a queue that never drains (overload,
 *    or a hot queue under Aggressive) it grows with the busy period.
 *  - service gap: now - t_poll[q], the previous poll of q. It is the time q
 *    waited without being visited while it held packets: the starvation
 *    interval. service gap <= drain gap.
 * Penalty skips do not poll and so move neither timestamp.
 */
#pragma once

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>

#include <rte_branch_prediction.h>
#include <rte_byteorder.h>
#include <rte_common.h>
#include <rte_cycles.h>
#include <rte_lcore.h>
#include <rte_mbuf.h>

#define QP_PAYLOAD_OFF 42
#define QP_MAGIC_BG 0x4251  /* "QB" read as little-endian u16 */
#define QP_MAGIC_LAT 0x4C51 /* "QL" */
#define QP_TREX_MAGIC 0xAB
#define QP_TREX_TRAILER 16
#define QP_MAX_QUEUES 2048
#define QP_HIST_BUCKETS 64 /* log2 of the gap in TSC cycles */
#define QP_WARMUP_S 3

enum qp_kind {
	QP_KIND_BG       = 1, /* sampled Zipf flow */
	QP_KIND_LAT      = 2, /* TRex latency stream with its trailer */
	QP_KIND_LAT_BAD  = 3, /* 'QL' marker but no TRex trailer found */
};

struct qp_rec {
	uint64_t tsc;     /* host TSC when the burst was read */
	uint64_t trex_ts; /* TRex TX timestamp (QP_KIND_LAT), 0 otherwise */
	uint32_t flow_id;
	uint32_t seq;     /* field-engine seq (BG) or TRex trailer seq (LAT) */
	uint16_t queue;
	uint8_t  kind;
	uint8_t  pos;     /* position in the burst */
	uint16_t nb_rx;   /* size of the burst */
	uint16_t lcore;
} __rte_packed;

struct qp_log {
	struct qp_rec *rec;
	uint64_t       n;
	uint64_t       cap;
	uint64_t       dropped;
	uint64_t       hist[QP_HIST_BUCKETS];     /* drain gaps weighted by packets */
	uint64_t       svc_hist[QP_HIST_BUCKETS]; /* service gaps weighted by packets */
} __rte_cache_aligned;

struct qp_queue {
	uint64_t t_drain;
	uint64_t t_poll;
	uint64_t max_gap;
	uint64_t max_svc;
	uint32_t pkts;  /* after the warmup */
	uint32_t polls; /* non-empty polls after the warmup */
} __rte_aligned(8);

static struct qp_log   qp_logs[RTE_MAX_LCORE];
static struct qp_queue qp_queues[QP_MAX_QUEUES];
static uint64_t        qp_t0;     /* end of the warmup */
static uint64_t        qp_t_last; /* last non-empty poll */
static unsigned        qp_debug_left = 4;

/* Allocate @records log entries split over the enabled lcores */
static inline void
qp_init(uint64_t records)
{
	unsigned lcore, n = rte_lcore_count();

	RTE_LCORE_FOREACH(lcore)
	{
		struct qp_log *log = &qp_logs[lcore];
		size_t         sz  = (records / n) * sizeof(struct qp_rec);

		log->rec = (struct qp_rec *)mmap(NULL, sz, PROT_READ | PROT_WRITE,
		                MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
		if (log->rec == MAP_FAILED)
			rte_exit(EXIT_FAILURE, "qprobe: cannot allocate %zu bytes\n", sz);
		log->cap = records / n;
	}
}

/* Call right before the polling loops start */
static inline void
qp_start(void)
{
	uint64_t now = rte_rdtsc();

	qp_t0 = now + QP_WARMUP_S * rte_get_tsc_hz();
	for (unsigned q = 0; q < QP_MAX_QUEUES; q++)
		qp_queues[q].t_drain = qp_queues[q].t_poll = now;
}

static __rte_always_inline void
qp_log_pkt(struct qp_log *log, struct rte_mbuf *m, uint64_t now, uint16_t queue, uint8_t pos,
           uint16_t nb_rx, unsigned lcore)
{
	const uint8_t *p   = rte_pktmbuf_mtod(m, const uint8_t *);
	uint16_t       len = rte_pktmbuf_data_len(m);
	uint16_t       magic;
	uint32_t       be;

	if (unlikely(len < QP_PAYLOAD_OFF + 2 + 4))
		return;
	memcpy(&magic, p + QP_PAYLOAD_OFF, sizeof(magic));
	if (likely(magic != QP_MAGIC_BG && magic != QP_MAGIC_LAT))
		return;

	struct qp_rec r = {
	    .tsc   = now,
	    .queue = queue,
	    .pos   = pos,
	    .nb_rx = nb_rx,
	    .lcore = (uint16_t)lcore,
	};
	memcpy(&be, p + QP_PAYLOAD_OFF + 2, sizeof(be));
	r.flow_id = rte_be_to_cpu_32(be);

	if (magic == QP_MAGIC_BG) {
		if (unlikely(len < QP_PAYLOAD_OFF + 11))
			return;
		memcpy(&be, p + QP_PAYLOAD_OFF + 6, sizeof(be));
		r.seq        = rte_be_to_cpu_32(be);
		uint8_t shift = p[QP_PAYLOAD_OFF + 10];
		if (shift >= 32 || (r.seq & ((1u << shift) - 1)) != 0)
			return;
		r.kind = QP_KIND_BG;
	} else {
		/* the trailer ends the frame; tolerate a CRC left in the buffer */
		const uint8_t *t = NULL;
		if (len >= QP_TREX_TRAILER && p[len - QP_TREX_TRAILER] == QP_TREX_MAGIC)
			t = p + len - QP_TREX_TRAILER;
		else if (len >= QP_TREX_TRAILER + 4 && p[len - QP_TREX_TRAILER - 4] == QP_TREX_MAGIC)
			t = p + len - QP_TREX_TRAILER - 4;

		if (unlikely(qp_debug_left > 0)) {
			qp_debug_left--;
			printf("qprobe sample len=%u tail=", len);
			for (int k = len > 20 ? len - 20 : 0; k < len; k++)
				printf("%02x", p[k]);
			printf("\n");
		}
		if (t != NULL) {
			memcpy(&r.seq, t + 4, sizeof(r.seq));
			memcpy(&r.trex_ts, t + 8, sizeof(r.trex_ts));
			r.kind = QP_KIND_LAT;
		} else {
			r.kind = QP_KIND_LAT_BAD;
		}
	}

	if (unlikely(log->n >= log->cap)) {
		log->dropped++;
		return;
	}
	log->rec[log->n++] = r;
}

/*
 * After every rte_eth_rx_burst() (Shared: before releasing the queue lock).
 * @queue is the absolute RX queue index.
 */
static __rte_always_inline void
qp_poll(unsigned lcore, uint16_t queue, struct rte_mbuf **pkts, uint16_t nb_rx, uint16_t max_burst)
{
	uint64_t         now = rte_rdtsc();
	struct qp_queue *qs  = &qp_queues[queue];
	struct qp_log   *log = &qp_logs[lcore];

	if (nb_rx > 0) {
		if (now > qp_t0) {
			uint64_t gap = now - qs->t_drain;
			uint64_t svc = now - qs->t_poll;
			if (gap > qs->max_gap)
				qs->max_gap = gap;
			if (svc > qs->max_svc)
				qs->max_svc = svc;
			log->hist[gap ? 63 - __builtin_clzll(gap) : 0] += nb_rx;
			log->svc_hist[svc ? 63 - __builtin_clzll(svc) : 0] += nb_rx;
			qs->pkts += nb_rx;
			qs->polls++;
			qp_t_last = now;
		}
		for (uint16_t j = 0; j < nb_rx; j++)
			qp_log_pkt(log, pkts[j], now, queue, (uint8_t)j, nb_rx, lcore);
	}
	if (nb_rx < max_burst)
		qs->t_drain = now;
	qs->t_poll = now;
}

/* Dump the log to @path and print the drain statistics */
static inline void
qp_finish(const char *path, unsigned n_queues)
{
	uint64_t records = 0, dropped = 0, hist[QP_HIST_BUCKETS] = {0}, svc_hist[QP_HIST_BUCKETS] = {0};
	unsigned lcore;

	RTE_LCORE_FOREACH(lcore)
	{
		records += qp_logs[lcore].n;
		dropped += qp_logs[lcore].dropped;
		for (int b = 0; b < QP_HIST_BUCKETS; b++) {
			hist[b] += qp_logs[lcore].hist[b];
			svc_hist[b] += qp_logs[lcore].svc_hist[b];
		}
	}

	printf("qprobe tsc_hz=%lu t0=%lu t_last=%lu records=%lu dropped=%lu\n",
	       rte_get_tsc_hz(), qp_t0, qp_t_last, records, dropped);
	for (unsigned q = 0; q < n_queues && q < QP_MAX_QUEUES; q++) {
		const struct qp_queue *qs = &qp_queues[q];
		printf("qdrain q=%u pkts=%u polls=%u max_cyc=%lu svc_cyc=%lu\n",
		       q, qs->pkts, qs->polls, qs->max_gap, qs->max_svc);
	}
	for (int b = 0; b < QP_HIST_BUCKETS; b++) {
		if (hist[b])
			printf("qhist b=%d pkts=%lu\n", b, hist[b]);
		if (svc_hist[b])
			printf("qsvc b=%d pkts=%lu\n", b, svc_hist[b]);
	}

	if (path == NULL)
		return;
	FILE *f = fopen(path, "wb");
	if (f == NULL) {
		printf("qprobe: cannot open %s\n", path);
		return;
	}
	/* header: magic, tsc_hz, t0, records */
	uint64_t hdr[4] = {0x003145424F525051ull /* "QPROBE1" */, rte_get_tsc_hz(), qp_t0, records};
	fwrite(hdr, sizeof(hdr), 1, f);
	RTE_LCORE_FOREACH(lcore)
	fwrite(qp_logs[lcore].rec, sizeof(struct qp_rec), qp_logs[lcore].n, f);
	fclose(f);
	printf("qprobe log written to %s\n", path);
}
