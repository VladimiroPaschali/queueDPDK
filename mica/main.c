/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2010-2016 Intel Corporation
 */

#include "ported-mica/table.h"
#include "rte_pmd_qdma.h"
#include "qflow.h"
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
#include <rte_spinlock.h>
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

/* Shared: an lcore polls a queue only while holding its lock */
static rte_spinlock_t rx_queue_locks[RTE_MAX_ETHPORTS][MAX_RX_QUEUE_PER_PORT];

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

static void
init_rx_queue_locks(void)
{
	for (uint16_t pid = 0; pid < RTE_MAX_ETHPORTS; pid++)
		for (uint16_t qid = 0; qid < MAX_RX_QUEUE_PER_PORT; qid++)
			rte_spinlock_init(&rx_queue_locks[pid][qid]);
}

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

struct five_tuple {
	uint32_t src_ip;
	uint32_t dst_ip;
	uint16_t src_port;
	uint16_t dst_port;
	uint8_t  proto;
} __attribute__((packed));

#define NUM_KEYS 10000000
static int VALUE_SIZE = 1400;
size_t     default_keys[NUM_KEYS];

/* GET and SET alternate: one toggle on a single core, one per lcore otherwise */
bool flag = false;
struct get_set_toggle_state {
	bool flag;
} __rte_cache_aligned;
static struct get_set_toggle_state get_set_toggle[RTE_MAX_LCORE];

static __rte_always_inline int
mica_process5tuple(uint8_t *pkt, const enum qflow_mode mode)
{
	struct five_tuple key;
	char              value[VALUE_SIZE];

	struct rte_ipv4_hdr *ip = (struct rte_ipv4_hdr *)(pkt + sizeof(struct rte_ether_hdr));

	struct rte_udp_hdr *udp =
	    (struct rte_udp_hdr *)(pkt + sizeof(struct rte_ether_hdr) + sizeof(struct rte_ipv4_hdr));

	/* costruzione 5-tupla */
	key.src_ip   = ip->src_addr;
	key.dst_ip   = ip->dst_addr;
	key.src_port = udp->src_port;
	key.dst_port = udp->dst_port;
	key.proto    = ip->next_proto_id;

	int do_get;
	if (mode == QFLOW_SINGLE) {
		do_get = flag;
		flag   = !flag;
	} else {
		unsigned lcore_id             = rte_lcore_id();
		do_get                        = get_set_toggle[lcore_id].flag;
		get_set_toggle[lcore_id].flag = !get_set_toggle[lcore_id].flag;
	}

	uint64_t key_hash = hash((const uint8_t *)&key, sizeof(key));

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
		       VALUE_SIZE);
	} else {

		memset(value, 'A', VALUE_SIZE - 1);
		value[VALUE_SIZE - 1] = '\0';

		if (!mehcached_set(0,
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
		       VALUE_SIZE);
	}

	return 0;
}

static __rte_always_inline int
mica_process_burst(struct rte_mbuf **pkts_burst, int nb_pkts, const enum qflow_mode mode)
{
	for (int i = 0; i < nb_pkts; i++) {

		uint8_t *payload;
		payload = rte_pktmbuf_mtod(pkts_burst[i], uint8_t *);
		mica_process5tuple(payload, mode);
	}
	return 0;
}

/* main processing loop */
uint64_t end_time = 0;

/*
 * Polling loop. mode is a constant in each main_loop_* wrapper, so the
 * compiler emits one specialised loop per multicore policy:
 *  - single core: polls every queue, prints statistics every second, sends
 *    each burst on TX queue 0 and measures throughput after a 3 s warmup;
 *  - Partitioned: polls a contiguous slice of queues, batches TX on its own
 *    TX queue;
 *  - Shared: polls every queue under a per-queue trylock, batches TX on its
 *    own TX queue.
 * Penalty, Aggressive and Latency apply on top of all three.
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

			if (mode == QFLOW_SHARED) {
				nb_rx = 0;
				if (!rte_spinlock_trylock(&rx_queue_locks[portid][i]))
					continue;
			}
			nb_rx = rte_eth_rx_burst(portid, i, pkts_burst, MAX_PKT_BURST);
			if (mode == QFLOW_SHARED)
				rte_spinlock_unlock(&rx_queue_locks[portid][i]);
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
					/* mbufs the NIC does not accept are not freed */
					rte_eth_tx_burst(portid, 0, pkts_burst, nb_rx);

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

			if (latency_mode && has_latency_queue) {
				/* back to the regular sweep after a latency poll */
				if (resume_i != -1 && i == latency_queue) {
					i        = resume_i - 1; // -1 per compensare i++
					resume_i = -1;
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

			if (penalty > 0 && max_loops == QFLOW_MAX_CONSECUTIVE_POLLS)
				hit[mode == QFLOW_PARTITIONED ? qi : i] = penalty * (nb_rx > MAX_PKT_BURST / 2);

			if (aggressive && nb_rx == MAX_PKT_BURST && max_loops > 0) {
				i--;
				max_loops--;
			} else {
				max_loops = QFLOW_MAX_CONSECUTIVE_POLLS;
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

static int
main_loop_single(__rte_unused void *dummy)
{
	return poll_loop(QFLOW_SINGLE);
}

static int
main_loop_partitioned(__rte_unused void *dummy)
{
	return poll_loop(QFLOW_PARTITIONED);
}

static int
main_loop_shared(__rte_unused void *dummy)
{
	return poll_loop(QFLOW_SHARED);
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
	       "  -v : print per-queue statistics\n" QFLOW_USAGE,
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
	static struct option lgopts[]     = {QFLOW_LONG_OPTIONS, {NULL, 0, 0, 0}};

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
	ret = parse_args(argc, argv);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "Invalid MICA parameters\n");

	nb_lcores = rte_lcore_count();
	if (qflow_check(&qflow, queues, nb_lcores) < 0)
		rte_exit(EXIT_FAILURE, "Invalid policy configuration\n");
	aggressive = qflow.aggressive;
	penalty    = qflow.penalty ? qflow.penalty_turns : 0;
	qflow_print(&qflow, queues);

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
	init_rx_queue_locks();

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

	table               = &table_o;
	size_t numa_nodes[] = {(size_t)-1};
	/*
	 * Single core: non-concurrent table, FIFO eviction. Multicore: concurrent
	 * table when more than one lcore polls, and no move-to-head on GET.
	 */
	const bool   concurrent_mica = active_io_lcores > 1;
	const double mth_threshold =
	    qflow.mode == QFLOW_SINGLE ? MEHCACHED_MTH_THRESHOLD_FIFO : 1.0;
	mehcached_table_init(table,
	                     (NUM_KEYS + MEHCACHED_ITEMS_PER_BUCKET - 1) / MEHCACHED_ITEMS_PER_BUCKET,
	                     1,
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

	/* Launch main loops */
	lcore_function_t *main_loop = main_loop_single;
	if (qflow.mode == QFLOW_PARTITIONED)
		main_loop = main_loop_partitioned;
	else if (qflow.mode == QFLOW_SHARED)
		main_loop = main_loop_shared;
	rte_eal_mp_remote_launch(main_loop, NULL, CALL_MAIN);

	RTE_LCORE_FOREACH_WORKER(lcore_id) { rte_eal_wait_lcore(lcore_id); }

	mehcached_table_free(table);

	if (qflow.mode == QFLOW_SINGLE) {
		print_stats();

		printf("measured RX packets: %.2f\n", (float)measured_packets_rx);
		printf("measured RX Throughput: %.2f\n",
		       (double)measured_packets_rx / ((double)end_time / (double)rte_get_timer_hz()));
		printf("measured time: %.2f seconds\n", (double)end_time / (double)rte_get_timer_hz());
	}

	return 0;
}
