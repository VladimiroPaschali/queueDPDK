/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2010-2016 Intel Corporation
 */

#include "ported-mica/table.h"
#include "rte_pmd_qdma.h"
#include "qflow.h"
#include "qbitstream.h"
#ifdef MICA_ADAPTIVE
#include "adaptive.h" /* common/: adaptive queue count */
#include "qdma_rss.h"
#endif
#ifdef MICA_QPROBE
#include "qprobe.h" /* common/: poll timestamps and marked packets */
#endif
#include <arpa/inet.h>
#include <locale.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <inttypes.h>
#include <sys/types.h>
#include <string.h>
#include <sys/queue.h>
#include <stdarg.h>
#include <errno.h>
#include <getopt.h>
#include <signal.h>

#include <rte_common.h>
#include <rte_byteorder.h>
#include <rte_log.h>
#include <rte_memory.h>
#include <rte_memcpy.h>
#include <rte_eal.h>
#include <rte_launch.h>
#include <rte_atomic.h>
#include <rte_cycles.h>
#include <rte_prefetch.h>
#include <rte_lcore.h>
#include <rte_per_lcore.h>
#include <rte_branch_prediction.h>
#include <rte_interrupts.h>
#include <rte_random.h>
#include <rte_debug.h>
#include <rte_ether.h>
#include <rte_ethdev.h>
#include <rte_mempool.h>
#include <rte_mbuf.h>
#include <rte_ip.h>
#include <rte_tcp.h>
#include <rte_udp.h>
#include <rte_string_fns.h>

#include <unistd.h>

#include "./ported-mica/hash.h"
#include "./ported-mica/mehcached.h"

#define MEMPOOL_CACHE_SIZE 256

static volatile bool force_quit;
uint16_t             portid = 0;

#define MAX_PKT_BURST 32
#define TX_BATCH_SIZE 512 /* multicore: max TX pkts accumulated across queues per sweep */
#define RTE_TEST_RX_DESC_DEFAULT 1024
#define RTE_TEST_TX_DESC_DEFAULT 1024
static uint16_t          nb_rxd = RTE_TEST_RX_DESC_DEFAULT;
static uint16_t          nb_txd = RTE_TEST_TX_DESC_DEFAULT;
static uint16_t          queues = 2;
static struct qflow_opts qflow;
/* Policy knobs read by the polling loop, derived from qflow */
static bool     aggressive = false;
static uint32_t penalty    = 0; /* 0 disables Penalty */
bool            verbose    = false;
uint32_t        skip_count = 0; // counter for skipped polls
uint32_t        empty      = 0;
uint32_t        total      = 0;
uint32_t        spin_time  = 0;
uint32_t        n_bursts   = 0;
uint32_t        spin_pkt   = 0;
uint32_t        n_pkts     = 0;

static uint64_t timer_period        = 3096000000; // 1 second
uint64_t        measured_packets_rx = 0;
/* lcores that poll RX queues: 1 on a single core, min(lcores, queues, TX queues) otherwise */
static uint16_t active_io_lcores = 1;

bool     after_warmup  = false; /* reset statistics after warmup time */
uint64_t measured_tick = 0;

#define MAX_RX_QUEUE_PER_LCORE 2048
#define MAX_RX_QUEUE_PER_PORT 2048

#ifdef MICA_QPROBE
/* qprobe log (see common/qprobe.h) */
static const char *qp_log_path = NULL;
static uint64_t    qp_records  = 8u << 20;
#define QP_LONG_OPTIONS {"log", 1, 0, 0}, {"log-records", 1, 0, 0},
#else
#define QP_LONG_OPTIONS
#endif

#ifdef MICA_ADAPTIVE
#define AD_LONG_OPTIONS ADAPTIVE_LONG_OPTIONS,
#define AD_USAGE ADAPTIVE_USAGE
#else
#define AD_LONG_OPTIONS
#define AD_USAGE ""
#endif

/* Per-port statistics struct */
struct port_statistics {
	uint64_t tx;
	uint64_t rx;
	uint64_t dropped;
} __rte_cache_aligned;
struct port_statistics port_statistics[RTE_MAX_ETHPORTS][MAX_RX_QUEUE_PER_LCORE];

/* Single core: per-queue Penalty counters (multicore keeps them per lcore) */
int                     queue_hit[MAX_RX_QUEUE_PER_LCORE] = {1};
struct mehcached_table  table_o;
struct mehcached_table *table;

#ifdef MICA_ADAPTIVE
/* Adaptive queue count on a single core (common/adaptive.h): the
 * mask register, the loop, the controller and the per-second statistics
 * follow the adaptive app (experiments/apps/adaptive/src/main.c), with MICA's work per burst */
void                        qdma_reg_write_usr(void *dev_hndl, uint32_t reg_offst, uint32_t val);
static struct adaptive_opts adaptive_opts;
static struct adaptive_ctl  adaptive_ctl;
static uint32_t             rss_mask_reg = UINT32_MAX;
static uint16_t             active_queues; /* queues the NIC steers to */
static uint16_t             polled_queues; /* active_queues plus those still draining */
static uint64_t             drain_until;   /* tsc before which draining queues stay polled */
static uint16_t             rx_ring_size = 1;
/* Polls of the active queues in the current second, and the cycles of the
 * sweeps that found work */
static uint64_t sec_polls, sec_empty, sec_full, sec_busy_cycles, sec_cycles;
/* Backlog left in a queue after a full poll, in the current second: largest
 * and how many full polls left more than half the ring */
static uint32_t sec_backlog_max, sec_backlog_half;
#endif

static void
print_stats(void)
{
	uint64_t        total_packets_dropped = 0, total_packets_tx = 0, total_packets_rx = 0;
	static uint64_t total_packets_tx_prev = 0, total_packets_rx_prev = 0,
	                total_packets_dropped_prev = 0;

	/* Static variables to store previous statistics */
	static uint64_t prev_tx[RTE_MAX_ETHPORTS][MAX_RX_QUEUE_PER_LCORE]      = {0};
	static uint64_t prev_rx[RTE_MAX_ETHPORTS][MAX_RX_QUEUE_PER_LCORE]      = {0};
	static uint64_t prev_dropped[RTE_MAX_ETHPORTS][MAX_RX_QUEUE_PER_LCORE] = {0};

	const char clr[]     = {27, '[', '2', 'J', '\0'};
	const char topLeft[] = {27, '[', '1', ';', '1', 'H', '\0'};

	/* Clear screen and move to top left */
	printf("%s%s", clr, topLeft);
	int active = 0;
	printf("\nPort statistics ====================================");

	for (int q = 0; q < queues; q++) {

		uint64_t diff_tx      = port_statistics[portid][q].tx - prev_tx[portid][q];
		uint64_t diff_rx      = port_statistics[portid][q].rx - prev_rx[portid][q];
		uint64_t diff_dropped = port_statistics[portid][q].dropped - prev_dropped[portid][q];

		if (diff_tx == 0 && diff_rx == 0 && diff_dropped == 0)
			continue;
		active++;
		if (verbose)
			printf("\nStatistics for port %u queue: %d ------------------------------"
			       "\nPackets sent:     %'20llu (diff: %'llu)"
			       "\nPackets received: %'20llu (diff: %'llu)"
			       "\nPackets dropped:  %'20llu (diff: %'llu)\n",
			       portid,
			       q,
			       (unsigned long long)port_statistics[portid][q].tx,
			       (unsigned long long)diff_tx,
			       (unsigned long long)port_statistics[portid][q].rx,
			       (unsigned long long)diff_rx,
			       (unsigned long long)port_statistics[portid][q].dropped,
			       (unsigned long long)diff_dropped);

		total_packets_dropped += port_statistics[portid][q].dropped;
		total_packets_tx += port_statistics[portid][q].tx;
		total_packets_rx += port_statistics[portid][q].rx;
		/* Update previous statistics */
		prev_tx[portid][q]      = port_statistics[portid][q].tx;
		prev_rx[portid][q]      = port_statistics[portid][q].rx;
		prev_dropped[portid][q] = port_statistics[portid][q].dropped;
	}
	printf("\nAggregate statistics ==============================="
	       "\nActive queues:          %'14d"
	       "\nTotal Packets sent:     %'14llu (diff: %'llu)"
	       "\nTotal Packets received: %'14llu (diff: %'llu)"
	       "\nTotal Packets dropped:  %'14llu (diff: %'llu)\n",
	       active,
	       (unsigned long long)total_packets_tx,
	       (unsigned long long)(total_packets_tx - total_packets_tx_prev),
	       (unsigned long long)total_packets_rx,
	       (unsigned long long)(total_packets_rx - total_packets_rx_prev),
	       (unsigned long long)total_packets_dropped,
	       (unsigned long long)(total_packets_dropped - total_packets_dropped_prev));
	printf("\nspin/sec (the poll read less than a BURST): %u (%u)\n",
	       spin_time,
	       spin_pkt / (spin_time + 1));
	printf("empty/sec (the poll read no packets): %u\n", empty);
	printf("not full (resets every second): %u\n", spin_time);

	printf("n_bursts (resets every second): %u\n", n_bursts);
	printf("pkts (resets every second): %u\n", n_pkts);
	printf("pkts/burst (resets every second): %.2f\n",
	       n_bursts == 0 ? 0 : (float)n_pkts / n_bursts);
	printf("empty/burst (resets every second): %.2f\n",
	       n_bursts == 0 ? 0 : (float)empty / n_bursts);
	printf("not-full/burst (resets every second): %.2f\n",
	       n_bursts == 0 ? 0 : (float)spin_time / n_bursts);
	if (penalty) {
		uint32_t total_polls = n_bursts + skip_count;
		printf("Skip stats: %u skipped, %u polled, %.1f%% skipped\n",
		       skip_count,
		       n_bursts,
		       total_polls > 0 ? 100.0 * skip_count / total_polls : 0);
		skip_count = 0;
	}

	active    = 0;
	empty     = 0;
	spin_time = 0;
	spin_pkt  = 0;
	total     = 0;
	n_bursts  = 0;
	n_pkts    = 0;
	qflow_print(&qflow, queues);
#ifdef MICA_ADAPTIVE
	/* the lines adaptive.py parses, as the adaptive app prints them */
	printf("poll mix: active polls %" PRIu64 ", empty %.1f%%, full %.1f%%, busy %.3f, "
	       "backlog max %.1f%%, over half %.3f%%\n",
	       sec_polls,
	       sec_polls ? 100.0 * (double)sec_empty / (double)sec_polls : 0.0,
	       sec_polls ? 100.0 * (double)sec_full / (double)sec_polls : 0.0,
	       sec_cycles ? (double)sec_busy_cycles / (double)sec_cycles : 0.0,
	       100.0 * sec_backlog_max / rx_ring_size,
	       sec_full ? 100.0 * (double)sec_backlog_half / (double)sec_full : 0.0);
	sec_polls = sec_empty = sec_full = sec_busy_cycles = sec_cycles = 0;
	sec_backlog_max = sec_backlog_half = 0;
	printf("RSS queues: %u (polled %u)", active_queues, polled_queues);
	if (adaptive_opts.enabled)
		printf(", adaptive signal, %" PRIu64 " changes", adaptive_ctl.changes);
	printf("\n");
#endif
	printf("\n====================================================\n");
	if (after_warmup)
		measured_tick++;
	/* Reset previous statistics */
	total_packets_tx_prev      = total_packets_tx;
	total_packets_rx_prev      = total_packets_rx;
	total_packets_dropped_prev = total_packets_dropped;

	printf("\n====================================================\n");
	mehcached_print_stats(table);
	fflush(stdout);
}

/* MICA stores and compares keys in 8-byte words (mehcached_memcpy8/memcmp8
 * round the length up), so a 13-byte key made it read 3 bytes past the
 * 5-tuple: whatever the stack held there became part of the key, and where
 * those bytes changed between packets (the stack layout of each build and poll
 * loop) no lookup matched and every SET appended a new item to the log. The key
 * is 16 bytes, the last 3 always zero. */
struct five_tuple {
	uint32_t src_ip;
	uint32_t dst_ip;
	uint16_t src_port;
	uint16_t dst_port;
	uint8_t  proto;
	uint8_t  pad[3];
} __attribute__((packed));
_Static_assert(sizeof(struct five_tuple) % 8 == 0, "MICA reads keys in 8-byte words");

#define NUM_KEYS 10000000
static int VALUE_SIZE = 1400;
size_t     default_keys[NUM_KEYS];

/* Bytes of the value copied into the packet: only what the 64-byte frame
 * holds (copying the 1400-byte value into the RX buffer made the rings outgrow
 * the LLC at ~16 queues). MICA_KEEP_PAYLOAD (the -lat, -qprobe and -adaptive
 * builds) copies nothing: the TRex latency trailer in the last 16 bytes of the
 * frame must come back intact */
#if defined(MICA_KEEP_PAYLOAD)
#define VALUE_COPY_SIZE 0
#else
#define VALUE_COPY_SIZE                                                                            \
	(64 - (int)(sizeof(struct rte_ether_hdr) + sizeof(struct rte_ipv4_hdr) +                      \
	            sizeof(struct rte_udp_hdr) + sizeof(size_t)))
#endif

/* GET and SET alternate: one toggle on a single core, one per lcore otherwise.
 * alloc_id is the log pool the lcore's SETs append to (MICA_POOL_PER_LCORE:
 * its lcore index, otherwise the single pool 0) */
bool flag = false;
struct get_set_toggle_state {
	bool    flag;
	uint8_t alloc_id;
} __rte_cache_aligned;
static struct get_set_toggle_state get_set_toggle[RTE_MAX_LCORE];

/* The 5-tuple of the packet, MICA's key, and its hash */
static __rte_always_inline uint64_t
mica_key(const uint8_t *pkt, struct five_tuple *key)
{
	const struct rte_ipv4_hdr *ip = (const struct rte_ipv4_hdr *)(pkt + sizeof(struct rte_ether_hdr));

	const struct rte_udp_hdr *udp =
	    (const struct rte_udp_hdr *)(pkt + sizeof(struct rte_ether_hdr) + sizeof(struct rte_ipv4_hdr));

	/* costruzione 5-tupla */
	key->src_ip   = ip->src_addr;
	key->dst_ip   = ip->dst_addr;
	key->src_port = udp->src_port;
	key->dst_port = udp->dst_port;
	key->proto    = ip->next_proto_id;
	key->pad[0] = key->pad[1] = key->pad[2] = 0;

	return hash((const uint8_t *)key, sizeof(*key));
}

/* The GET or SET of one packet, whose key and hash mica_key() computed */
static __rte_always_inline int
mica_request(uint8_t *pkt, const struct five_tuple *key_p, uint64_t key_hash, const enum qflow_mode mode)
{
	const struct five_tuple key = *key_p;
	char                    value[VALUE_SIZE];

	int     do_get;
	uint8_t alloc_id = 0;
	if (mode == QFLOW_SINGLE) {
		do_get = flag;
		flag   = !flag;
	} else {
		unsigned lcore_id             = rte_lcore_id();
		do_get                        = get_set_toggle[lcore_id].flag;
		get_set_toggle[lcore_id].flag = !get_set_toggle[lcore_id].flag;
		alloc_id                      = get_set_toggle[lcore_id].alloc_id;
	}

	if (do_get) {

		size_t value_length = sizeof(value);

		if (mehcached_get(0,
		                  table,
		                  key_hash,
		                  (const uint8_t *)&key,
		                  sizeof(key),
		                  (uint8_t *)&value,
		                  &value_length,
		                  NULL,
		                  false))
			assert(value_length == sizeof(value));

		memcpy(pkt + sizeof(struct rte_ether_hdr) + sizeof(struct rte_ipv4_hdr) +
		           sizeof(struct rte_udp_hdr) + sizeof(size_t),
		       &value,
		       VALUE_COPY_SIZE);
	} else {

		memset(value, 'A', VALUE_SIZE - 1);
		value[VALUE_SIZE - 1] = '\0';

		if (!mehcached_set(alloc_id,
		                   table,
		                   key_hash,
		                   (const uint8_t *)&key,
		                   sizeof(key),
		                   (const uint8_t *)&value,
		                   sizeof(value),
		                   0,
		                   true))
			assert(false);

		memcpy(pkt + sizeof(struct rte_ether_hdr) + sizeof(struct rte_ipv4_hdr) +
		           sizeof(struct rte_udp_hdr) + sizeof(size_t),
		       &value,
		       VALUE_COPY_SIZE);
	}

	return 0;
}

/*
 * MICA's request pipeline, which the original port did not use: hash every key of the
 * burst and prefetch its bucket, then the item the bucket points to, then run
 * the requests, so the L3 latency of one request overlaps with the others.
 * mehcached_prefetch_alloc() fetches the first 2 lines of the item (prefetching
 * all its 23 lines was slower: ~300 prefetches per burst).
 */
static __rte_always_inline int
mica_process_burst_prefetch(struct rte_mbuf **pkts_burst, int nb_pkts, const enum qflow_mode mode)
{
	struct five_tuple               keys[MAX_PKT_BURST];
	uint64_t                        hashes[MAX_PKT_BURST];
	struct mehcached_prefetch_state pf[MAX_PKT_BURST];

#ifdef MICA_PF_HDR
	/* The first touch of a packet header (mica_key) is an L2 miss served by the LLC where
	 * the NIC wrote it, about one per packet and not overlapped: prefetching the headers of
	 * the whole burst first overlaps them */
	for (int i = 0; i < nb_pkts; i++)
		rte_prefetch0(rte_pktmbuf_mtod(pkts_burst[i], void *));
#endif
	for (int i = 0; i < nb_pkts; i++) {
		hashes[i] = mica_key(rte_pktmbuf_mtod(pkts_burst[i], uint8_t *), &keys[i]);
		mehcached_prefetch_table(table, hashes[i], &pf[i]);
	}
	for (int i = 0; i < nb_pkts; i++)
		mehcached_prefetch_alloc(&pf[i]);
	for (int i = 0; i < nb_pkts; i++)
		mica_request(rte_pktmbuf_mtod(pkts_burst[i], uint8_t *), &keys[i], hashes[i], mode);
	return 0;
}

static __rte_always_inline int
mica_process_burst(struct rte_mbuf **pkts_burst, int nb_pkts, const enum qflow_mode mode)
{
	return mica_process_burst_prefetch(pkts_burst, nb_pkts, mode);
}

/* main processing loop */
uint64_t end_time = 0;

/*
 * Polling loop. mode is a constant in each main_loop_* wrapper, so the
 * compiler emits one specialised loop per multicore policy:
 *  - single core: polls every queue, prints statistics every second, sends
 *    each burst on TX queue 0 and measures throughput after a 3 s warmup;
 *  - Partitioned: polls a contiguous slice of queues, batches TX on its own
 *    TX queue.
 * Penalty, Aggressive and Latency apply on top of both.
 */
static __rte_always_inline int
poll_loop(const enum qflow_mode mode)
{
	struct rte_mbuf *pkts_burst[MAX_PKT_BURST];
	struct rte_mbuf *tx_batch[TX_BATCH_SIZE];
	int              queue_hit_local[MAX_RX_QUEUE_PER_LCORE] __rte_cache_aligned;
	unsigned         lcore_id;
	uint64_t         prev_tsc, diff_tsc, cur_tsc, timer_tsc, end_warmup = 0;
	int              i, nb_rx;
	timer_tsc = 0;
	prev_tsc  = 0;
	lcore_id  = rte_lcore_id();
	int ret;

	const bool latency_mode   = qflow.latency;
	const int  latency_period = qflow.latency_period;
	const int  latency_queue  = qflow.latency_queue;
	int        latency_count  = 0;
	bool       inject_queue   = false;
	int        resume_i       = -1;

	const unsigned lcore_idx       = rte_lcore_index(lcore_id);
	uint16_t       first_queue     = 0;
	uint16_t       assigned_queues = queues;
	const uint16_t tx_queue_id     = (uint16_t)lcore_idx;
	if (mode != QFLOW_SINGLE) {
		first_queue     = queues;
		assigned_queues = 0;
		if (lcore_idx < active_io_lcores) {
			first_queue     = 0;
			assigned_queues = queues;
		}
		if (mode == QFLOW_PARTITIONED && lcore_idx < active_io_lcores) {
			const uint16_t queues_per_lcore = queues / active_io_lcores;
			const uint16_t extra_queues     = queues % active_io_lcores;
			first_queue =
			    (uint16_t)(lcore_idx * queues_per_lcore + RTE_MIN(lcore_idx, extra_queues));
			assigned_queues = (uint16_t)(queues_per_lcore + (lcore_idx < extra_queues));
		}
	}
	const uint16_t last_queue = first_queue + assigned_queues;
	const bool     has_latency_queue =
        (latency_queue >= first_queue) && (latency_queue < last_queue);

	if (assigned_queues == 0) {
		printf("entering main loop on lcore %u (idle, no RX queues assigned)\n", lcore_id);
		return 0;
	}

#ifdef MICA_POOL_PER_LCORE
	if (mode != QFLOW_SINGLE)
		get_set_toggle[lcore_id].alloc_id = (uint8_t)lcore_idx;
#endif

	/* Penalty counters: global on a single core, per lcore otherwise */
	int *hit = (mode == QFLOW_SINGLE) ? queue_hit : queue_hit_local;
	if (mode != QFLOW_SINGLE)
		for (int qi = 0; qi < assigned_queues; qi++)
			queue_hit_local[qi] = penalty;

	if (mode == QFLOW_SINGLE)
		printf("entering main loop on lcore %u\n", lcore_id);
	else
		printf("entering main loop on lcore %u (rx queues [%u, %u), tx queue %u)\n",
		       lcore_id,
		       first_queue,
		       last_queue,
		       tx_queue_id);
	fflush(stdout);

	uint64_t start_time  = rte_rdtsc();
	uint64_t warmup_time = 3 * rte_get_timer_hz();
	uint64_t stop_time   = (10 * rte_get_timer_hz()) + warmup_time;

	while (!force_quit) {

		cur_tsc = rte_rdtsc();

		diff_tsc = cur_tsc - prev_tsc;
		/* if timer is enabled */
		if (timer_period > 0) {
			/* advance the timer */
			timer_tsc += diff_tsc;
			/* if timer has reached its timeout */
			if (unlikely(timer_tsc >= timer_period)) {
				/* do this only on main core */
				if (lcore_id == rte_get_main_lcore()) {
					if (mode == QFLOW_SINGLE)
						print_stats();
					/* reset the timer */
					timer_tsc = 0;
				}
			}
		}
		prev_tsc = cur_tsc;

		/*
		 * Read packet from RX queues
		 */
		int max_loops = QFLOW_MAX_CONSECUTIVE_POLLS;
		int tx_total  = 0;

		for (i = first_queue; i < last_queue; ++i) {
			/* Partitioned indexes its counters relative to its slice and
			 * keeps that index across a latency diversion */
			const int qi = i - first_queue;

			if ((penalty > 0) && (hit[qi] < penalty)) {
				hit[qi]++;
				if (mode == QFLOW_SINGLE)
					skip_count++;
				continue;
			}

			if (latency_mode && has_latency_queue && inject_queue) {
				resume_i     = i;
				i            = latency_queue;
				inject_queue = false;
			}

			nb_rx = rte_eth_rx_burst(portid, i, pkts_burst, MAX_PKT_BURST);
#ifdef MICA_QPROBE
			qp_poll(lcore_id, (uint16_t)i, pkts_burst, (uint16_t)nb_rx, MAX_PKT_BURST);
#endif
			if (mode == QFLOW_SINGLE) {
				n_bursts++;
				n_pkts += nb_rx;
			}

			if (nb_rx > 0) {

				ret = mica_process_burst(pkts_burst, nb_rx, mode);
				if (ret == 1) {
					printf("Received stop signal, exiting main loop\n");
					if (mode == QFLOW_SINGLE)
						end_time = cur_tsc - end_warmup;
					force_quit = true;
					break;
				}
				if (mode == QFLOW_SINGLE) {
					uint16_t nb_tx = rte_eth_tx_burst(portid, 0, pkts_burst, nb_rx);
					for (int j = nb_tx; j < nb_rx; j++)
						rte_pktmbuf_free(pkts_burst[j]);

					port_statistics[portid][i].rx += nb_rx;
					if (after_warmup) {
						measured_packets_rx += nb_rx;
					}
				} else {
					if (unlikely(tx_total + nb_rx > TX_BATCH_SIZE)) {
						uint16_t nb_tx = rte_eth_tx_burst(portid, tx_queue_id, tx_batch, tx_total);
						for (int j = nb_tx; j < tx_total; j++)
							rte_pktmbuf_free(tx_batch[j]);
						tx_total = 0;
					}
					rte_memcpy(&tx_batch[tx_total], pkts_burst, nb_rx * sizeof(struct rte_mbuf *));
					tx_total += nb_rx;
				}
			}

			/* Penalty and Aggressive act on regular polls only: after a latency
			 * poll i already points back into the sweep */
			bool latency_poll = false;
			if (latency_mode && has_latency_queue) {
				/* back to the regular sweep after a latency poll */
				if (resume_i != -1 && i == latency_queue) {
					i            = resume_i - 1; // -1 per compensare i++
					resume_i     = -1;
					latency_poll = true;
				}
				/* count only regular queues */
				else if (i != latency_queue) {
					latency_count++;
					if (latency_count == latency_period) {
						latency_count = 0;
						inject_queue  = true;
					}
				}
			}

			if (!latency_poll) {
				if (penalty > 0 && max_loops == QFLOW_MAX_CONSECUTIVE_POLLS)
					hit[mode == QFLOW_PARTITIONED ? qi : i] = penalty * (nb_rx > MAX_PKT_BURST / 2);

				if (aggressive && nb_rx == MAX_PKT_BURST && max_loops > 0) {
					i--;
					max_loops--;
				} else {
					max_loops = QFLOW_MAX_CONSECUTIVE_POLLS;
				}
			}

			if (mode == QFLOW_SINGLE) {
				if (nb_rx < MAX_PKT_BURST) {
					spin_time++;
				}
				spin_pkt += nb_rx;
				if (nb_rx == 0) {
					empty++;
				}
			}
		}

		/* Single TX for all packets collected across all queues this sweep */
		if (mode != QFLOW_SINGLE && tx_total > 0) {
			uint16_t nb_tx = rte_eth_tx_burst(portid, tx_queue_id, tx_batch, tx_total);
			for (int j = nb_tx; j < tx_total; j++)
				rte_pktmbuf_free(tx_batch[j]);
		}

		if (mode == QFLOW_SINGLE) {
			if ((cur_tsc - start_time) > stop_time) {
				end_time = cur_tsc - end_warmup;
			} else if (cur_tsc - start_time > warmup_time) { // 3 seconds
				printf("Warmup finished\n");
				after_warmup = true;
				end_warmup   = cur_tsc;
				warmup_time  = 1000 * rte_get_timer_hz(); // reset warmup time
			}
		}
	}
	if (mode == QFLOW_SINGLE)
		end_time = rte_rdtsc() - end_warmup;
	return 0;
}

#ifdef MICA_ADAPTIVE
/***********************adaptive queue count******************************/

/* After a shrink, the dropped queues are polled for at least this long */
#define DRAIN_MIN_US 200

static uint32_t
rss_reta_first(uint16_t pid)
{
	struct rte_eth_rss_reta_entry64 reta_conf[2048 / RTE_RETA_GROUP_SIZE];
	struct rte_eth_dev_info         dev_info;

	if (rte_eth_dev_info_get(pid, &dev_info) != 0)
		return UINT32_MAX;
	for (unsigned i = 0; i < RTE_DIM(reta_conf); i++)
		reta_conf[i].mask = ~0ULL;
	if (rte_eth_dev_rss_reta_query(pid, reta_conf, dev_info.reta_size) != 0)
		return UINT32_MAX;
	return reta_conf[0].reta[0];
}

/* The mask register is 16 bits wide and its first read after startup can
 * return garbage: read twice and keep the low half (experiments/apps/adaptive/src/main.c) */
static uint32_t
rss_mask_read(struct rte_eth_dev *dev, uint32_t reg)
{
	(void)qdma_reg_read_usr(dev, reg);
	return qdma_reg_read_usr(dev, reg) & 0xffff;
}

/*
 * The mask register is the one qdma_dev_reta_update() writes, at the address
 * the PMD derives from its func_id (qdma_rss.c); no other address is touched.
 * Check that it holds queues - 1 and that a change to it shows up in
 * rte_eth_dev_rss_reta_query(), then restore it.
 */
static int
rss_mask_locate(uint16_t pid, uint16_t nq)
{
	struct rte_eth_dev *dev   = &rte_eth_devices[pid];
	const uint32_t      reg   = qdma_rss_mask_reg(pid);
	const uint32_t      probe = nq / 2 - 1;
	const uint32_t      old   = rss_mask_read(dev, reg);
	uint32_t            seen;

	if (old != nq - 1u) {
		printf("RSS mask register 0x%x holds %u, expected %u\n", reg, old, nq - 1u);
		return -1;
	}
	qdma_reg_write_usr(dev, reg, probe);
	seen = rss_reta_first(pid);
	qdma_reg_write_usr(dev, reg, old);
	if (seen != probe) {
		printf("RSS mask register 0x%x: wrote %u, reta query read %u\n", reg, probe, seen);
		return -1;
	}
	rss_mask_reg = reg;
	return 0;
}

/*
 * Change the number of queues the NIC steers to. Growing starts polling the
 * new queues before the NIC fills them; shrinking keeps polling the dropped
 * queues until a sweep finds them empty after DRAIN_MIN_US.
 */
static void
set_active_queues(uint16_t n, uint64_t now)
{
	struct rte_eth_dev *dev = &rte_eth_devices[portid];

	if (n > active_queues) {
		for (uint16_t q = active_queues; q < n; q++)
			queue_hit[q] = penalty;
		active_queues = n;
		if (polled_queues < n)
			polled_queues = n;
		qdma_reg_write_usr(dev, rss_mask_reg, n - 1u);
	} else if (n < active_queues) {
		qdma_reg_write_usr(dev, rss_mask_reg, n - 1u);
		active_queues = n;
		drain_until   = now + DRAIN_MIN_US * rte_get_timer_hz() / US_PER_S;
	}
}

static void
adaptive_step(uint64_t now,
              uint64_t cycles,
              uint64_t pkts,
              uint64_t polls,
              uint64_t empty_polls,
              double   backlog_share)
{
	const unsigned prev = adaptive_ctl.cur_log;
	const unsigned next =
	    adaptive_epoch(&adaptive_ctl, polls, empty_polls, backlog_share, cycles, pkts);

	if (next == prev)
		return;
	set_active_queues((uint16_t)(1u << next), now);
	printf("adaptive: %u -> %u queues after signal epoch: %.1f cycles/pkt (base %.1f), "
	       "empty %.1f%%, backlog share %.2f%%\n",
	       1u << prev,
	       1u << next,
	       pkts ? (double)cycles / (double)pkts : 0.0,
	       adaptive_ctl.grow_cost,
	       100.0 * adaptive_ctl.last_empty,
	       100.0 * adaptive_ctl.last_backlog);
}

static void
print_adaptive_summary(void)
{
	uint64_t total_epochs = 0;

	for (unsigned l = 0; l <= ADAPTIVE_MAX_LOG; l++)
		total_epochs += adaptive_ctl.epochs[l];
	printf("\nadaptive: %" PRIu64 " changes, final %u queues, epochs per queue count:\n",
	       adaptive_ctl.changes,
	       active_queues);
	for (unsigned l = 0; l <= ADAPTIVE_MAX_LOG; l++)
		if (adaptive_ctl.epochs[l])
			printf("adaptive: %4u queues %8" PRIu64 " (%6.2f%%)\n",
			       1u << l,
			       adaptive_ctl.epochs[l],
			       100.0 * (double)adaptive_ctl.epochs[l] / (double)total_epochs);
}

/*
 * Single-core loop of the adaptive variant, as poll_loop() of the adaptive
 * app: sweeps the polled queues (the active ones plus those still draining
 * after a shrink, polled every sweep), prints statistics every second, sends
 * each burst on TX queue 0 and measures throughput after a 3 s warmup.
 * Penalty and Aggressive apply to the active queues. With --latency-queue,
 * every latency-period regular polls one poll goes to the queue of the
 * latency flow among the active ones. With --adaptive, the controller runs at
 * the end of every epoch on the cycles of the sweeps that found work.
 */
static int
adaptive_poll_loop(void)
{
	struct rte_mbuf *pkts_burst[MAX_PKT_BURST];
	const unsigned   lcore_id = rte_lcore_id();
	uint64_t         prev_tsc, diff_tsc, cur_tsc = 0, timer_tsc = 0, end_warmup = 0;
	int              i, nb_rx, ret;
	int             *hit = queue_hit;

	const bool     adaptive     = adaptive_opts.enabled;
	const uint64_t epoch_period = (uint64_t)adaptive_opts.epoch_ms * rte_get_timer_hz() / MS_PER_S;
	uint64_t       epoch_tsc = 0, epoch_cycles = 0, epoch_pkts = 0;
	uint64_t       epoch_polls = 0, epoch_empty = 0, epoch_full = 0, epoch_half = 0;
	uint32_t       sweep_pkts = 0, sweep_polls = 0, sweep_empty = 0, sweep_full = 0, sweep_half = 0;

	const bool latency_mode   = qflow.latency;
	const int  latency_period = qflow.latency_period;
	int        latency_count  = 0, resume_i = -1;
	bool       inject_queue   = false;

	printf("entering main loop on lcore %u\n", lcore_id);
	fflush(stdout);

	uint64_t start_time  = rte_rdtsc();
	uint64_t warmup_time = 3 * rte_get_timer_hz();
	uint64_t stop_time   = (10 * rte_get_timer_hz()) + warmup_time;
	prev_tsc             = start_time;

	while (!force_quit) {
		bool slow_path = false;

		cur_tsc  = rte_rdtsc();
		diff_tsc = cur_tsc - prev_tsc;
		/* the previous sweep counts for the controller, and its poll outcomes,
		 * only if it found work: an idle core spinning on empty queues wastes
		 * nothing */
		if (sweep_pkts > 0) {
			epoch_cycles += diff_tsc;
			epoch_pkts += sweep_pkts;
			sec_busy_cycles += diff_tsc;
			epoch_polls += sweep_polls;
			epoch_empty += sweep_empty;
			epoch_full += sweep_full;
			epoch_half += sweep_half;
			sec_polls += sweep_polls;
			sec_empty += sweep_empty;
			sec_full += sweep_full;
			sweep_pkts = 0;
		}
		sec_cycles += diff_tsc;
		sweep_polls = sweep_empty = sweep_full = sweep_half = 0;
		if (timer_period > 0) {
			timer_tsc += diff_tsc;
			if (unlikely(timer_tsc >= timer_period)) {
				print_stats();
				timer_tsc = 0;
				slow_path = true;
			}
		}
		if (adaptive) {
			epoch_tsc += diff_tsc;
			if (unlikely(epoch_tsc >= epoch_period)) {
				adaptive_step(cur_tsc, epoch_cycles, epoch_pkts, epoch_polls, epoch_empty,
				              epoch_full ? (double)epoch_half / (double)epoch_full : 0.0);
				epoch_tsc    = 0;
				epoch_cycles = 0;
				epoch_pkts   = 0;
				epoch_polls = epoch_empty = epoch_full = epoch_half = 0;
				slow_path    = true;
			}
		}
		/* keep statistics and controller time out of the next sweep */
		prev_tsc = slow_path ? rte_rdtsc() : cur_tsc;

		int        max_loops  = QFLOW_MAX_CONSECUTIVE_POLLS;
		const int  sweep_end  = polled_queues;
		const int  active_end = active_queues;
		bool       drained    = true;
		const bool draining   = sweep_end > active_end;
		/* --latency-queue names the queue with -q queues: the NIC steers with
		 * hash & (active - 1), so with fewer active queues the flow is on its
		 * low bits */
		const int latency_queue = latency_mode ? qflow.latency_queue & (active_end - 1) : -1;

		for (i = 0; i < sweep_end; ++i) {
			bool drain_queue = i >= active_end;

			if (!drain_queue && (penalty > 0) && (hit[i] < penalty)) {
				hit[i]++;
				skip_count++;
				continue;
			}
			if (unlikely(inject_queue)) {
				resume_i     = i;
				i            = latency_queue;
				inject_queue = false;
				drain_queue  = false;
			}

			nb_rx = rte_eth_rx_burst(portid, (uint16_t)i, pkts_burst, MAX_PKT_BURST);
			n_bursts++;
			n_pkts += nb_rx;
			sweep_pkts += nb_rx;
			if (drain_queue && nb_rx > 0)
				drained = false;

			if (nb_rx > 0) {
				ret = mica_process_burst(pkts_burst, nb_rx, QFLOW_SINGLE);
				if (ret == 1) {
					printf("Received stop signal, exiting main loop\n");
					end_time   = cur_tsc - end_warmup;
					force_quit = true;
					break;
				}
				uint16_t nb_tx = rte_eth_tx_burst(portid, 0, pkts_burst, nb_rx);
				for (int j = nb_tx; j < nb_rx; j++)
					rte_pktmbuf_free(pkts_burst[j]);

				port_statistics[portid][i].rx += nb_rx;
				if (after_warmup)
					measured_packets_rx += nb_rx;
			}

			/* a latency poll is not a poll of the sweep: no Penalty, Aggressive
			 * or controller statistics, and i goes back into the sweep */
			bool latency_poll = false;
			if (latency_mode) {
				if (resume_i != -1 && i == latency_queue) {
					i            = resume_i - 1;
					resume_i     = -1;
					latency_poll = true;
				} else if (!drain_queue && i != latency_queue && ++latency_count == latency_period) {
					latency_count = 0;
					inject_queue  = true;
				}
			}

			if (!drain_queue && !latency_poll) {
				sweep_polls++;
				sweep_empty += nb_rx == 0;
				if (nb_rx == MAX_PKT_BURST) {
					/* what the full burst left behind in the ring */
					const uint32_t b = qdma_rx_backlog(portid, (uint16_t)i);

					sweep_full++;
					sweep_half += 2 * b > rx_ring_size;
					if (b > sec_backlog_max)
						sec_backlog_max = b;
					sec_backlog_half += 2 * b > rx_ring_size;
				}
				if (penalty > 0 && max_loops == QFLOW_MAX_CONSECUTIVE_POLLS)
					hit[i] = penalty * (nb_rx > MAX_PKT_BURST / 2);

				if (aggressive && nb_rx == MAX_PKT_BURST && max_loops > 0) {
					i--;
					max_loops--;
				} else {
					max_loops = QFLOW_MAX_CONSECUTIVE_POLLS;
				}
			}

			if (nb_rx < MAX_PKT_BURST)
				spin_time++;
			spin_pkt += nb_rx;
			if (nb_rx == 0)
				empty++;
		}

		/* the dropped queues are empty and the NIC no longer fills them */
		if (draining && drained && cur_tsc >= drain_until)
			polled_queues = active_queues;

		if ((cur_tsc - start_time) > stop_time) {
			end_time = cur_tsc - end_warmup;
		} else if (cur_tsc - start_time > warmup_time) { // 3 seconds
			printf("Warmup finished\n");
			after_warmup = true;
			end_warmup   = cur_tsc;
			warmup_time  = 1000 * rte_get_timer_hz(); // reset warmup time
		}
	}
	end_time = rte_rdtsc() - end_warmup;
	return 0;
}

/***********************end of adaptive queue count******************************/
#endif

static int
main_loop_single(__rte_unused void *dummy)
{
#ifdef MICA_ADAPTIVE
	return adaptive_poll_loop();
#else
	return poll_loop(QFLOW_SINGLE);
#endif
}

static int
main_loop_partitioned(__rte_unused void *dummy)
{
	return poll_loop(QFLOW_PARTITIONED);
}

static unsigned int
parse_queues(const char *q_arg)
{
	char         *end = NULL;
	unsigned long n;

	n = strtoul(q_arg, &end, 10);

	if ((q_arg[0] == '\0') || (end == NULL) || (*end != '\0'))
		return 0;
	if (n == 0)
		return 0;

	return n;
}

static void
print_usage(const char *prgname)
{
	printf("%s [EAL options] -- [-q N] [-d N] [-v] [policy options]\n"
	       "  -q N: number of RX queues\n"
	       "  -d N: RX descriptors per queue (power of 2, >= 64)\n"
	       "  -v : print per-queue statistics\n" QFLOW_USAGE AD_USAGE,
	       prgname);
}

static int
parse_args(int argc, char **argv)
{
	int   opt, ret, option_index;
	char *prgname = argv[0];

	static const char short_options[] = "q:" /* queues       */
	                                    "d:" /* descriptors  */
	                                    "v"; /* verbose      */
	static struct option lgopts[]     = {QFLOW_LONG_OPTIONS, QP_LONG_OPTIONS AD_LONG_OPTIONS{NULL, 0, 0, 0}};

	/* reset getopt internal state (important when relaunching app) */
	optind         = 1;
	char **argvopt = argv;

	while ((opt = getopt_long(argc, argvopt, short_options, lgopts, &option_index)) != EOF) {

		switch (opt) {

		case 'd':
			nb_rxd = parse_queues(optarg);
			if (nb_rxd < 64 || (nb_rxd & (nb_rxd - 1)) != 0) {
				printf("invalid number of descriptors\n");
				return -1;
			}
			printf("Number of descriptors per port: %u\n", nb_rxd);
			break;

		case 'q':
			queues = parse_queues(optarg);
			if (queues == 0) {
				printf("invalid number of queues\n");
				return -1;
			}
			printf("Number of queues per port: %u\n", queues);
			break;

		case 'v':
			verbose = true;
			break;

		case 0:
#ifdef MICA_QPROBE
			if (!strcmp(lgopts[option_index].name, "log")) {
				qp_log_path = optarg;
				break;
			}
			if (!strcmp(lgopts[option_index].name, "log-records")) {
				qp_records = strtoull(optarg, NULL, 10);
				if (qp_records == 0) {
					printf("invalid log-records value:\"%s\"\n", optarg);
					return -1;
				}
				break;
			}
#endif
#ifdef MICA_ADAPTIVE
			ret = adaptive_parse_long_opt(&adaptive_opts, lgopts[option_index].name, optarg);
			if (ret < 0) {
				print_usage(prgname);
				return -1;
			}
			if (ret > 0)
				break;
#endif
			if (qflow_parse_long_opt(&qflow, lgopts[option_index].name, optarg) > 0)
				break;
			print_usage(prgname);
			return -1;

		default:
			print_usage(prgname);
			return -1;
		}
	}

	if (optind >= 0)
		argv[optind - 1] = prgname;

	ret    = optind - 1;
	optind = 1; /* reset getopt lib */
	return ret;
}

#define PKT_SIZE 64 // Dimensione totale del pacchetto
struct rte_mbuf *
create_udp_packet(struct rte_mempool   *mbuf_pool,
                  struct rte_ether_addr src_mac,
                  struct rte_ether_addr dst_mac,
                  uint32_t              src_ip,
                  uint32_t              dst_ip,
                  uint16_t              src_port,
                  uint16_t              dst_port,
                  uint32_t              magic_value)
{
	struct rte_mbuf *mbuf = rte_pktmbuf_alloc(mbuf_pool);
	if (!mbuf) {
		return NULL;
	}

	// Alloca spazio per il pacchetto
	char *pkt_data = rte_pktmbuf_append(mbuf, PKT_SIZE);
	if (!pkt_data) {
		rte_pktmbuf_free(mbuf);
		return NULL;
	}

	// Puntatori ai vari header
	struct rte_ether_hdr *eth     = (struct rte_ether_hdr *)pkt_data;
	struct rte_ipv4_hdr  *ip      = (struct rte_ipv4_hdr *)(eth + 1);
	struct rte_udp_hdr   *udp     = (struct rte_udp_hdr *)(ip + 1);
	uint8_t              *payload = (uint8_t *)(udp + 1);

	// --- Ethernet header ---
	rte_ether_addr_copy(&dst_mac, &eth->d_addr);
	rte_ether_addr_copy(&src_mac, &eth->s_addr);
	eth->ether_type = rte_cpu_to_be_16(RTE_ETHER_TYPE_IPV4);

	// --- IPv4 header ---
	ip->version_ihl     = (4 << 4) | (sizeof(struct rte_ipv4_hdr) / 4);
	ip->type_of_service = 0;
	ip->total_length    = rte_cpu_to_be_16(PKT_SIZE - sizeof(struct rte_ether_hdr));
	ip->packet_id       = rte_cpu_to_be_16(0);
	ip->fragment_offset = 0;
	ip->time_to_live    = 64;
	ip->next_proto_id   = IPPROTO_UDP;
	ip->src_addr        = src_ip;
	ip->dst_addr        = dst_ip;
	ip->hdr_checksum    = 0;
	ip->hdr_checksum    = rte_ipv4_cksum(ip);

	// --- UDP header ---
	udp->src_port = rte_cpu_to_be_16(src_port);
	udp->dst_port = rte_cpu_to_be_16(dst_port);
	udp->dgram_len =
	    rte_cpu_to_be_16(PKT_SIZE - sizeof(struct rte_ether_hdr) - sizeof(struct rte_ipv4_hdr));
	udp->dgram_cksum = 0; // opzionale, calcolo checksum se vuoi

	// --- Payload ---
	for (int i = 0; i < PKT_SIZE - sizeof(struct rte_ether_hdr) - sizeof(struct rte_ipv4_hdr) -
	                        sizeof(struct rte_udp_hdr);
	     i++) {
		payload[i] = (uint8_t)i; // dati dummy
	}

	size_t offset = sizeof(*eth) + sizeof(*ip) + sizeof(*udp);
	offset        = (offset + sizeof(uint64_t)) & ~(sizeof(uint64_t) - 1);
	// pktgen legge in little-endian
	uint32_t magic = magic_value;

	memcpy(pkt_data + offset, &magic, sizeof(uint32_t));

	return mbuf;
}

struct rte_mbuf *
create_latency_packet(struct rte_mempool *mbuf_pool, uint32_t magic_value)
{
	struct rte_ether_addr src_mac = {{0x02, 0x00, 0x00, 0x00, 0x00, 0x01}};
	struct rte_ether_addr dst_mac = {{0xe0, 0xeb, 0xd3, 0x78, 0x95, 0x8d}};

	uint32_t src_ip   = inet_addr("192.168.0.1");
	uint32_t dst_ip   = inet_addr("192.168.1.1");
	uint16_t src_port = 1234;
	uint16_t dst_port = 1028;

	struct rte_mbuf *pkt = create_udp_packet(
	    mbuf_pool, src_mac, dst_mac, src_ip, dst_ip, src_port, dst_port, magic_value);

	if (!pkt) {
		rte_exit(EXIT_FAILURE, "Errore creazione pacchetto\n");
	}
	return pkt;
}

static void
signal_handler(int signum)
{
	if (signum == SIGINT || signum == SIGTERM) {
		printf("\n\nSignal %d received, preparing to exit...\n", signum);
		force_quit = true;
	}
}

int
main(int argc, char **argv)
{
	uint16_t            nb_lcores;
	unsigned            lcore_id;
	struct rte_mempool *mbuf_pool                           = NULL;
	struct rte_mempool *mbuf_pool_per_lcore[RTE_MAX_LCORE] = {NULL};

	setlocale(LC_NUMERIC, ""); // Usa locale di sistema per i separatori

	signal(SIGINT, signal_handler);
	signal(SIGTERM, signal_handler);

	int ret = rte_eal_init(argc, argv);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "EAL init failed\n");
	argc -= ret;
	argv += ret;

	/* parse application arguments (after the EAL ones) */
	qflow_opts_init(&qflow);
#ifdef MICA_ADAPTIVE
	adaptive_opts_init(&adaptive_opts);
#endif
	ret = parse_args(argc, argv);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "Invalid MICA parameters\n");

	nb_lcores = rte_lcore_count();
	if (qflow_check(&qflow, queues, nb_lcores) < 0)
		rte_exit(EXIT_FAILURE, "Invalid policy configuration\n");
	aggressive = qflow.aggressive;
	penalty    = qflow.penalty ? qflow.penalty_turns : 0;
	qflow_print(&qflow, queues);
#ifdef MICA_ADAPTIVE
	if (nb_lcores != 1 || qflow.mode != QFLOW_SINGLE)
		rte_exit(EXIT_FAILURE, "adaptive runs on one lcore\n");
	if (adaptive_check(&adaptive_opts, queues) < 0)
		rte_exit(EXIT_FAILURE, "Invalid adaptive configuration\n");
	if (adaptive_opts.snapshot_ms)
		printf("--snapshot-ms is ignored by MICA\n");
	if (adaptive_opts.enabled)
		printf("Adaptive: %u to %u queues, start %u, epoch %u ms, threshold %u%%, hold %u\n",
		       adaptive_opts.min,
		       queues,
		       adaptive_opts.start,
		       adaptive_opts.epoch_ms,
		       adaptive_opts.threshold_pct,
		       adaptive_opts.hold);
	if (adaptive_opts.enabled)
		printf("Adaptive signal mode: double above %g%% of full polls leaving half the ring, halve "
		       "above %u%% empty polls or below %g%%\n",
		       adaptive_opts.backlog_high_pct,
		       adaptive_opts.empty_high_pct,
		       adaptive_opts.backlog_low_pct);
	if (adaptive_opts.enabled && adaptive_opts.search)
		printf("Adaptive search: a flat grow jumps 2, 4, 8 octaves before it is undone, "
		       "trying fewer queues is judged, no signal for %u epochs probes fewer\n",
		       ADAPTIVE_PROBE_EPOCHS);
#endif

	uint16_t nb_ports = rte_eth_dev_count_avail();
	if (nb_ports == 0)
		rte_exit(EXIT_FAILURE, "No available ports\n");
	if (queues > MAX_RX_QUEUE_PER_PORT)
		rte_exit(EXIT_FAILURE, "Too many RX queues: %u > %u\n", queues, MAX_RX_QUEUE_PER_PORT);

	struct rte_eth_dev_info dev_info;
	ret = rte_eth_dev_info_get(portid, &dev_info);
	if (ret != 0)
		rte_exit(EXIT_FAILURE,
		         "Error during getting device (port %u) info: %s\n",
		         portid,
		         strerror(-ret));

	if (qflow.mode != QFLOW_SINGLE) {
		active_io_lcores = RTE_MIN(nb_lcores, (uint16_t)queues);
		active_io_lcores = RTE_MIN(active_io_lcores, dev_info.max_tx_queues);
		if (active_io_lcores == 0)
			rte_exit(EXIT_FAILURE,
			         "Invalid lcore/queue setup: lcores=%u queues=%u max_tx_queues=%u\n",
			         nb_lcores,
			         queues,
			         dev_info.max_tx_queues);
	}

	/* ---- Mempool ---- */
	if (qflow.mode == QFLOW_PARTITIONED) {
		/* One pool per polling lcore, sized for the queues it owns */
		uint16_t queues_per_lcore = queues / active_io_lcores;
		uint16_t extra_queues     = queues % active_io_lcores;
		unsigned lc;
		uint16_t lc_idx = 0;
		RTE_LCORE_FOREACH(lc)
		{
			if (lc_idx >= active_io_lcores)
				break;
			uint16_t lc_queues = queues_per_lcore + (lc_idx < extra_queues ? 1 : 0);
			uint32_t nb_mbufs  = RTE_MAX(
                (uint32_t)(lc_queues * (nb_rxd + MAX_PKT_BURST) + nb_txd + MEMPOOL_CACHE_SIZE),
                8192U);
			char pool_name[32];
			snprintf(pool_name, sizeof(pool_name), "mbuf_pool_%u", lc);
			mbuf_pool_per_lcore[lc] = rte_pktmbuf_pool_create(pool_name,
			                                                  nb_mbufs,
			                                                  MEMPOOL_CACHE_SIZE,
			                                                  0,
			                                                  RTE_MBUF_DEFAULT_BUF_SIZE,
			                                                  rte_lcore_to_socket_id(lc));
			if (!mbuf_pool_per_lcore[lc])
				rte_exit(EXIT_FAILURE, "Cannot create mbuf pool for lcore %u\n", lc);
			lc_idx++;
		}
		mbuf_pool = mbuf_pool_per_lcore[rte_get_main_lcore()];
	} else {
		/* The single-core build read the lcore count before EAL init, so
		 * its pool had no per-lcore cache term */
		uint16_t pool_lcores = qflow.mode == QFLOW_SINGLE ? 0 : nb_lcores;
		int      nb_mbufs    = RTE_MAX(
            queues * 1 * (nb_rxd + nb_txd + MAX_PKT_BURST + pool_lcores * MEMPOOL_CACHE_SIZE),
            8192U);
		mbuf_pool = rte_pktmbuf_pool_create("mbuf_pool",
		                                    nb_mbufs,
		                                    MEMPOOL_CACHE_SIZE,
		                                    0,
		                                    RTE_MBUF_DEFAULT_BUF_SIZE,
		                                    rte_socket_id());
		if (!mbuf_pool)
			rte_exit(EXIT_FAILURE, "Cannot create mbuf pool\n");
	}

	for (int i = 0; i < MAX_RX_QUEUE_PER_LCORE; i++) {
		queue_hit[i] = penalty;
	}

	/* Configure port */
	struct rte_eth_conf port_conf = {0};
	port_conf.rxmode.mq_mode      = ETH_MQ_RX_NONE;
	uint16_t n_tx_queue           = active_io_lcores;

	ret = rte_eth_dev_configure(portid, (uint16_t)queues, n_tx_queue, &port_conf);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "Configure failed\n");

	// QueueDPDK setup
	// Check correct bitstream
	struct rte_eth_dev *dev       = &rte_eth_devices[portid];
	uint32_t            reg_offst = 0; // timestamp;
	uint32_t            val       = qdma_reg_read_usr(dev, reg_offst);
	// Timestamp--> QDMA Reg (0x0) Value: 0x0940907F
	printf("Timestamp--> QDMA Reg (0x%X) Value: 0x%X\n", reg_offst, val);
	if (val != 0x940907F) {
		printf("wrong bitstream\n");
		return 0;
	}
	// which variant: C2H prefetch cache and completion coalescing depth
	qbitstream_print(dev);

	struct rte_eth_rss_reta_entry64 reta_conf[2048 / RTE_RETA_GROUP_SIZE];
	for (int i = 0; i < dev_info.reta_size / RTE_RETA_GROUP_SIZE; i++) {
		reta_conf[i].mask = ~0LL;
		for (int j = 0; j < RTE_RETA_GROUP_SIZE; j++)
			reta_conf[i].reta[j] = 0;
	}
	// salva l'indir table sul device
	ret = rte_eth_dev_rss_reta_update(portid, reta_conf, dev_info.reta_size);
	if (ret < 0)
		// stampa errore
		rte_exit(EXIT_FAILURE, "Cannot set RSS REA: err=%d, port=%u\n", ret, portid);

	rte_eth_dev_rss_reta_query(portid, reta_conf, dev_info.reta_size);
	printf("pre-queues %u\n", queues);
	printf("reta-queues %u\n", reta_conf[0].reta[0] + 1);
	if (queues != reta_conf[0].reta[0] + 1)
		rte_exit(EXIT_FAILURE,
		         "Cannot get correct number of queues: %d != %d\n",
		         queues,
		         reta_conf[0].reta[0] + 1);
	// End QueueDPDK setup

	/* RX queue */
	{
		struct rte_eth_rxconf rxq_conf = dev_info.default_rxconf;
		rxq_conf.offloads              = port_conf.rxmode.offloads;
		uint16_t queues_per_lcore      = queues / active_io_lcores;
		uint16_t extra_queues          = queues % active_io_lcores;

		for (int x = 0; x < queues; x++) {
			int diag = rte_pmd_qdma_set_queue_mode(portid, x, RTE_PMD_QDMA_STREAMING_MODE);
			if (diag < 0)
				rte_exit(EXIT_FAILURE,
				         "rte_pmd_qdma_set_queue_mode : "
				         "Passing of STREAMING_MODE "
				         "failed\n");

			struct rte_mempool *pool = mbuf_pool;
			if (qflow.mode == QFLOW_PARTITIONED) {
				/* Find which lcore owns this queue */
				uint16_t owner_lc_idx;
				uint16_t boundary = extra_queues * (queues_per_lcore + 1);
				if (x < boundary)
					owner_lc_idx = (uint16_t)(x / (queues_per_lcore + 1));
				else
					owner_lc_idx = (uint16_t)(extra_queues + (x - boundary) / queues_per_lcore);

				unsigned owner_lc = 0;
				uint16_t tmp_idx  = 0;
				RTE_LCORE_FOREACH(owner_lc)
				{
					if (tmp_idx == owner_lc_idx)
						break;
					tmp_idx++;
				}
				pool = mbuf_pool_per_lcore[owner_lc];
			}

			ret = rte_eth_rx_queue_setup(
			    portid, x, nb_rxd, rte_eth_dev_socket_id(portid), &rxq_conf, pool);
			if (ret < 0)
				rte_exit(EXIT_FAILURE, "rte_eth_rx_queue_setup:err=%d, port=%u\n", ret, portid);
		}
	}

	/* TX queue: one per polling lcore */
	{
		struct rte_eth_txconf *txconf = &dev_info.default_txconf;
		txconf->offloads              = port_conf.txmode.offloads;
		for (uint16_t queue_id = 0; queue_id < n_tx_queue; queue_id++) {
			ret = rte_eth_tx_queue_setup(
			    portid, queue_id, nb_txd, rte_eth_dev_socket_id(portid), txconf);
			if (ret < 0)
				rte_exit(EXIT_FAILURE, "TX queue setup failed (queue=%u, err=%d)\n", queue_id, ret);
		}
	}

	/* Start port */
	ret = rte_eth_dev_start(portid);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "Device start failed\n");

	printf("Port %u initialized.\n", portid);

	/* Flush packets left in the RFC queues by the previous run */
	sleep(3);
	int send_packets = rte_log2_u32(queues) + 1;
	printf("numero di pacchetti %d\n", send_packets);
	for (int i = 0; i < send_packets; i++) {
		// send error packet 0xf00dcafc
		uint32_t magic_value = 0xf00dcafc;
		// creazione pacchetto latency
		struct rte_mbuf *pkt = create_latency_packet(mbuf_pool, magic_value);
		// --- Invia pacchetto su porta 0, queue 0 ---
		uint16_t nb_tx = rte_eth_tx_burst(portid, 0, &pkt, 1);
		if (nb_tx < 1) {
			printf("Invio fallito, liberando mbuf\n");
			rte_pktmbuf_free(pkt);
			rte_exit(EXIT_FAILURE, "Errore Invio pacchetto clear\n");

		} else {
			printf("Pacchetto inviato con successo\n");
		}
	}

	// mica init
	static uint64_t umem_size            = 4096;
	const size_t    page_size            = 1048576 * 2; // 2MB hugepages
	const size_t    num_numa_nodes       = 1;
	const size_t    num_pages_to_try     = umem_size;
	const size_t    num_pages_to_reserve = umem_size - umem_size / 8;
	size_t          alloc_overhead       = sizeof(struct mehcached_item);

	mehcached_shm_init(page_size, num_numa_nodes, num_pages_to_try, num_pages_to_reserve);

	table = &table_o;
	size_t numa_nodes[MEHCACHED_MAX_POOLS];
	for (size_t i = 0; i < MEHCACHED_MAX_POOLS; i++)
		numa_nodes[i] = (size_t)-1;
	/*
	 * Single core: non-concurrent table. ported-mica/table.h swaps the two
	 * thresholds (upstream MICA has FIFO 1.0, LRU 0.0), so the FIFO name would
	 * give full LRU (every GET moves its item to the head of the log): the
	 * threshold passed is 1.0, i.e. true FIFO. Multicore: concurrent
	 * table when more than one lcore polls, and no move-to-head on GET.
	 * Every SET locks the log pool it appends to and holds the lock while it
	 * writes the value; with one pool that lock, and the pool's head/tail on
	 * the same cache line that every GET reads, serialize the lcores. The
	 * MICA_POOL_PER_LCORE build gives every polling lcore its own pool.
	 */
	const bool concurrent_mica = active_io_lcores > 1;
#ifdef MICA_POOL_PER_LCORE
	const size_t num_pools = concurrent_mica ? active_io_lcores : 1;
	RTE_VERIFY(num_pools <= MEHCACHED_MAX_POOLS);
#else
	const size_t num_pools = 1;
#endif
	const double mth_threshold = 1.0;
	mehcached_table_init(table,
	                     (NUM_KEYS + MEHCACHED_ITEMS_PER_BUCKET - 1) / MEHCACHED_ITEMS_PER_BUCKET,
	                     num_pools,
	                     NUM_KEYS * /*MEHCACHED_ROUNDUP64*/ (alloc_overhead + 8 + 8),
	                     concurrent_mica,
	                     concurrent_mica,
	                     concurrent_mica,
	                     numa_nodes[0],
	                     numa_nodes,
	                     mth_threshold);
	assert(table);

	char default_value[VALUE_SIZE];
	memset(default_value, 'A', VALUE_SIZE - 1);
	default_value[VALUE_SIZE - 1] = '\0';

	for (size_t i = 0; i < NUM_KEYS; i++) {
		size_t key      = i;
		default_keys[i] = key;

		uint64_t key_hash = hash((const uint8_t *)&key, sizeof(key));
		if (!mehcached_set(0,
		                   table,
		                   key_hash,
		                   (const uint8_t *)&key,
		                   sizeof(key),
		                   (const uint8_t *)&default_value,
		                   sizeof(default_value),
		                   0,
		                   false))
			assert(false);
	}

#ifdef MICA_ADAPTIVE
	/* RX queues the NIC starts with */
	rx_ring_size  = qdma_rx_ring_size(portid, 0);
	active_queues = queues;
	polled_queues = queues;
	if (adaptive_opts.enabled) {
		if (rss_mask_locate(portid, queues) < 0)
			rte_exit(EXIT_FAILURE, "Cannot find the RSS mask register of port %u\n", portid);
		printf("RSS mask register: 0x%x\n", rss_mask_reg);
		adaptive_ctl_init(&adaptive_ctl, &adaptive_opts, queues);
		set_active_queues((uint16_t)adaptive_opts.start, rte_rdtsc());
	}
#endif

	/* Launch main loops */
	lcore_function_t *main_loop = main_loop_single;
	if (qflow.mode == QFLOW_PARTITIONED)
		main_loop = main_loop_partitioned;
#ifdef MICA_QPROBE
	qp_init(qp_records);
	qp_start();
#endif
	rte_eal_mp_remote_launch(main_loop, NULL, CALL_MAIN);

	RTE_LCORE_FOREACH_WORKER(lcore_id) { rte_eal_wait_lcore(lcore_id); }
#ifdef MICA_QPROBE
	qp_finish(qp_log_path, queues);
#endif

	mehcached_table_free(table);

	if (qflow.mode == QFLOW_SINGLE) {
		print_stats();

		printf("measured RX packets: %.2f\n", (float)measured_packets_rx);
		printf("measured RX Throughput: %.2f\n",
		       (double)measured_packets_rx / ((double)end_time / (double)rte_get_timer_hz()));
		printf("measured time: %.2f seconds\n", (double)end_time / (double)rte_get_timer_hz());
	}
#ifdef MICA_ADAPTIVE
	if (adaptive_opts.enabled)
		print_adaptive_summary();
#endif

	return 0;
}
