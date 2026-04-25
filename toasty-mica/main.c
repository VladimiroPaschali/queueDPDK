/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2010-2016 Intel Corporation
 */

#include "ported-mica/table.h"
#include "rte_pmd_qdma.h"
#include <arpa/inet.h>
// #include <cstddef>
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
#include <rte_acl.h>

#include <cmdline_parse.h>
#include <cmdline_parse_etheraddr.h>
#include <unistd.h>

#include "./ported-mica/hash.h"
#include "./ported-mica/mehcached.h"

#define MAX_JUMBO_PKT_LEN 9600
#define MEMPOOL_CACHE_SIZE 256

static volatile bool force_quit;
uint16_t             portid = 0;

#define MAX_PKT_BURST 32
#define RTE_TEST_RX_DESC_DEFAULT 1024
#define RTE_TEST_TX_DESC_DEFAULT 1024
static uint16_t nb_rxd     = RTE_TEST_RX_DESC_DEFAULT;
static uint16_t nb_txd     = RTE_TEST_TX_DESC_DEFAULT;
static uint16_t queues     = 2;
static bool     aggressive = false;
bool            verbose    = false;
uint32_t        skip       = 0;
uint32_t        skip_count = 0;  // counter for skipped polls
uint32_t        empty      = 0;
uint32_t        total      = 0;
uint32_t        spin_time  = 0;
uint32_t        n_bursts   = 0;
uint32_t        spin_pkt   = 0;
uint32_t        n_pkts     = 0;

static uint64_t timer_period        = 3096000000; // 1 second
uint64_t        measured_packets_rx = 0;

bool     after_warmup  = false; /* reset statistics after warmup time */
uint64_t measured_tick = 0;

#define MAX_RX_QUEUE_PER_LCORE 2048
#define MAX_TX_QUEUE_PER_PORT RTE_MAX_ETHPORTS
#define MAX_RX_QUEUE_PER_PORT 128

/* Per-port statistics struct */
struct port_statistics {
	uint64_t tx;
	uint64_t rx;
	uint64_t dropped;
} __rte_cache_aligned;
struct port_statistics port_statistics[RTE_MAX_ETHPORTS][MAX_RX_QUEUE_PER_LCORE];

#define MAX_LCORE_PARAMS 1024
struct lcore_params {
	uint16_t port_id;
	uint8_t  queue_id;
	uint8_t  lcore_id;
} __rte_cache_aligned;

static struct lcore_params lcore_params_array[MAX_LCORE_PARAMS];
static struct lcore_params lcore_params_array_default[] = {
    {0, 0, 2},
    {0, 1, 2},
    {0, 2, 2},
    {1, 0, 2},
    {1, 1, 2},
    {1, 2, 2},
    {2, 0, 2},
    {3, 0, 3},
    {3, 1, 3},
};

static struct lcore_params *lcore_params = lcore_params_array_default;
static uint16_t             nb_lcore_params =
    sizeof(lcore_params_array_default) / sizeof(lcore_params_array_default[0]);

static struct rte_eth_conf port_conf = {
    .rxmode =
        {
            .mq_mode = ETH_MQ_RX_RSS,
        },
    .rx_adv_conf =
        {
            .rss_conf =
                {
                    .rss_key = NULL,
                    .rss_hf  = ETH_RSS_IP | ETH_RSS_UDP | ETH_RSS_TCP | ETH_RSS_SCTP,
                },
        },
    .txmode =
        {
            .mq_mode = ETH_MQ_TX_NONE,
        },
};


int queue_hit[2048] = {1};
struct mehcached_table  table_o;
struct mehcached_table *table;
static void
print_stats(void)
{
	uint64_t        total_packets_dropped = 0, total_packets_tx = 0, total_packets_rx = 0;
	static uint64_t total_packets_tx_prev = 0, total_packets_rx_prev = 0,
	                total_packets_dropped_prev = 0;

	unsigned portid;

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

	// for (portid = 0; portid < RTE_MAX_ETHPORTS; portid++) {

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
	// }
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
	// printf("miss: %u\n", miss);
	printf("empty/sec (the poll read no packets): %u\n", empty);
	printf("not full (resets every second): %u\n", spin_time);

	// printf("total (burst in a second): %u (%u)\n", total, spin_pkt / (total + 1));
	// printf("packets/burst: %u\n", n_bursts == 0 ? 0 : spin_pkt / n_bursts);
	printf("n_bursts (resets every second): %u\n", n_bursts);
	printf("pkts (resets every second): %u\n", n_pkts);
	printf("pkts/burst (resets every second): %.2f\n",
	       n_bursts == 0 ? 0 : (float)n_pkts / n_bursts);
	printf("empty/burst (resets every second): %.2f\n",
	       n_bursts == 0 ? 0 : (float)empty / n_bursts);
	printf("not-full/burst (resets every second): %.2f\n",
	       n_bursts == 0 ? 0 : (float)spin_time / n_bursts);
	if (skip) {
		uint32_t total_polls = n_bursts + skip_count;
		printf("Skip stats: %u skipped, %u polled, %.1f%% skipped\n", 
		       skip_count, n_bursts, total_polls > 0 ? 100.0 * skip_count / total_polls : 0);
		skip_count = 0;
	}
	
	active    = 0;
	empty     = 0;
	spin_time = 0;
	spin_pkt  = 0;
	total     = 0;
	n_bursts  = 0;
	n_pkts    = 0;
	if (aggressive)
		printf("With aggressive policy\n");
	else
		printf("Without aggressive policy\n");
	if (skip)
		printf("With skip policy: %d\n", skip);
	else
		printf("Without skip policy\n");
	printf("RX queues: %u\n", queues);
	printf("\n====================================================\n");
	// if (after_warmup)
	// 	measured_packets_rx += (total_packets_rx - total_packets_rx_prev);
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

static inline uint32_t
str_to_u32(const char s[4])
{
	uint32_t v;
	memcpy(&v, s, 4); // copia esatti 4 byte
	return v;
}
static int
check_packet_magic(struct rte_mbuf *m, uint32_t expected_magic)
{
	struct rte_ether_hdr *eth = rte_pktmbuf_mtod(m, struct rte_ether_hdr *);
	struct rte_ipv4_hdr  *ip  = (struct rte_ipv4_hdr *)(eth + 1);

	if (ip->src_addr != inet_addr("192.168.0.1") || ip->dst_addr != inet_addr("192.168.1.1") ||
	    ip->next_proto_id != IPPROTO_UDP) {
		return -1; // Not the expected IPs
	}
	struct rte_udp_hdr *udp = (struct rte_udp_hdr *)(ip + 1);
	if (rte_be_to_cpu_16(udp->dst_port) != 1028 || rte_be_to_cpu_16(udp->src_port) != 1234) {
		return -1; // Not the expected ports
	}
	uint8_t *payload = (uint8_t *)(udp + 1);

	size_t offset = sizeof(*eth) + sizeof(*ip) + sizeof(*udp);
	offset        = (offset + sizeof(uint64_t)) & ~(sizeof(uint64_t) - 1);

	uint32_t received_magic;
	memcpy(&received_magic, (uint8_t *)eth + offset, sizeof(uint32_t));
	// printf("Received magic: 0x%08x, Expected magic: 0x%08x\n", received_magic, expected_magic);

	if (received_magic == expected_magic) {
		return 0; // Magic value matches
	} else {
		return -1; // Magic value does not match
	}
}

struct five_tuple {
    uint32_t src_ip;
    uint32_t dst_ip;
    uint16_t src_port;
    uint16_t dst_port;
    uint8_t  proto;
} __attribute__((packed));


#define NUM_KEYS 10000000
// static int VALUE_SIZE = 256;
static int VALUE_SIZE = 1400;
size_t     default_keys[NUM_KEYS];
int        keys_index = 0;
bool       flag       = false;
static int get_ratio = 50; // 70% GET, 30% STORE

static int
mica_process5tuple(uint8_t *pkt)
{
    struct five_tuple key;
    char value[VALUE_SIZE];

    struct rte_ether_hdr *eth =
        (struct rte_ether_hdr *)pkt;

    struct rte_ipv4_hdr *ip =
        (struct rte_ipv4_hdr *)(pkt + sizeof(struct rte_ether_hdr));

    struct rte_udp_hdr *udp =
        (struct rte_udp_hdr *)(pkt +
            sizeof(struct rte_ether_hdr) +
            sizeof(struct rte_ipv4_hdr));

    /* costruzione 5-tupla */
    key.src_ip   = ip->src_addr;
    key.dst_ip   = ip->dst_addr;
    key.src_port = udp->src_port;
    key.dst_port = udp->dst_port;
    key.proto    = ip->next_proto_id;

    // int do_get = (rand() % 100) < get_ratio;
	int do_get = flag;
	flag   = !flag;

    uint64_t key_hash =
        hash((const uint8_t *)&key, sizeof(key));

	// printf("key hash: %lu\n", key_hash);
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

		memcpy(pkt+ sizeof(struct rte_ether_hdr) + sizeof(struct rte_ipv4_hdr) +
		             sizeof(struct rte_udp_hdr) + sizeof(size_t), &value, VALUE_SIZE);


        // printf("GET flow: %u:%u -> %u:%u proto=%u\n",
        //        ip->src_addr,
        //        rte_be_to_cpu_16(udp->src_port),
        //        ip->dst_addr,
        //        rte_be_to_cpu_16(udp->dst_port),
        //        ip->next_proto_id);
    }

    else {

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

        // printf("SET flow: %u:%u -> %u:%u proto=%u\n",
        //        ip->src_addr,
        //        rte_be_to_cpu_16(udp->src_port),
        //        ip->dst_addr,
        //        rte_be_to_cpu_16(udp->dst_port),
        //        ip->next_proto_id);
		memcpy(pkt + sizeof(struct rte_ether_hdr) + sizeof(struct rte_ipv4_hdr) +
		             sizeof(struct rte_udp_hdr) + sizeof(size_t), &value, VALUE_SIZE);

    }

    return 0;
}


static int
mica_process_burst(struct rte_mbuf **pkts_burst, int nb_pkts)
{
	for (int i = 0; i < nb_pkts; i++) {

		uint8_t *payload;
		payload = rte_pktmbuf_mtod(pkts_burst[i], uint8_t *);
		mica_process5tuple(payload);
	}
	return 0;
}

/* main processing loop */
uint64_t end_time = 0;

static int
main_loop(__rte_unused void *dummy)
{
	struct rte_mbuf   *pkts_burst[MAX_PKT_BURST];
	unsigned           lcore_id;
	uint64_t           prev_tsc, diff_tsc, cur_tsc, timer_tsc, end_warmup;
	int                i, nb_rx;
	uint8_t            queueid;
	struct lcore_conf *qconf;
	int                socketid;
	timer_tsc = 0;
	prev_tsc  = 0;
	lcore_id  = rte_lcore_id();
	socketid  = rte_lcore_to_socket_id(lcore_id);
	int ret;

	int  latency_mode   = 0;
	int  latency_period = 4; // ogni 4 code
	int  latency_count  = 0;
	bool inject_queue   = false;
	int  resume_i       = -1;
	int  latency_queue  = 27; // coda latency con 64 code totali
	// int latency_queue = queues -1 ; // ultima coda simulata latency

	printf("entering main loop on lcore %u\n", lcore_id);

	uint64_t start_time  = rte_rdtsc();
	uint64_t warmup_time = 3 * rte_get_timer_hz();
	uint64_t stop_time   = (10 * rte_get_timer_hz()) + warmup_time;
	// uint64_t stop_time   = (2 * rte_get_timer_hz()) + warmup_time;

	while (!force_quit) {

		cur_tsc = rte_rdtsc();

		/*
		 * TX burst queue drain
		 */
		diff_tsc = cur_tsc - prev_tsc;
		/* if timer is enabled */
		if (timer_period > 0) {
			/* advance the timer */
			timer_tsc += diff_tsc;
			/* if timer has reached its timeout */
			if (unlikely(timer_tsc >= timer_period)) {
				/* do this only on main core */
				if (lcore_id == rte_get_main_lcore()) {
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
		int max_loops = 100;

		for (i = 0; i < queues; ++i) {

			if ((skip > 0) && (queue_hit[i] < skip)) {
				queue_hit[i]++;
				skip_count++;
				continue;
			}

			if (latency_mode && inject_queue) {
				resume_i     = i;             // salva dove dovevi andare
				i            = latency_queue; // inietta la coda specificata
				inject_queue = false;
			}

			nb_rx = rte_eth_rx_burst(portid, i, pkts_burst, MAX_PKT_BURST);
			n_bursts++;
			n_pkts += nb_rx;

			if (nb_rx > 0) {

				ret = mica_process_burst(pkts_burst, nb_rx);
				if (ret == 1) {
					printf("Received stop signal, exiting main loop\n");
					end_time   = cur_tsc - end_warmup;
					force_quit = true;
					break;
				}

				uint16_t nb_tx = rte_eth_tx_burst(portid, 0, pkts_burst, nb_rx);

				port_statistics[portid][i].rx += nb_rx;
				if (after_warmup) {
					measured_packets_rx += nb_rx;
				}
			}
			if (latency_mode) {

				/* se abbiamo appena fatto una 27 iniettata */
				if (resume_i != -1 && i == latency_queue) {
					i        = resume_i - 1; // -1 per compensare i++
					resume_i = -1;
				}

				/* conta SOLO le code normali, ESCLUDI la 27 */
				else if (i != latency_queue) {
					latency_count++;
					if (latency_count == latency_period) {
						latency_count = 0;
						inject_queue  = true;
					}
				}
			}

			if (skip > 0 && max_loops == 100)
				queue_hit[i] = skip * (nb_rx > MAX_PKT_BURST / 2);

			if (aggressive && nb_rx == MAX_PKT_BURST && max_loops > 0) {
				if (i > 0) {
					i--;
				}
				max_loops--;
			} else {
				max_loops = 100;
			}

			if (nb_rx < MAX_PKT_BURST) {
				spin_time++;
			}
			spin_pkt += nb_rx;
			if (nb_rx == 0) {
				empty++;
			}
		}
		if ((cur_tsc - start_time) > stop_time) { // 13 seconds) {
			end_time = cur_tsc - end_warmup;
			// uncomment below to stop after 13 seconds of measurement
			// break;
		} else if (cur_tsc - start_time > warmup_time) { // 3 seconds
			// rte_eth_stats_reset(portid); // skip the first 3 seconds
			printf("Warmup finished\n");
			after_warmup = true;
			end_warmup   = cur_tsc;
			warmup_time  = 1000 * rte_get_timer_hz(); // reset warmup time
		}
	}
	end_time = rte_rdtsc() - end_warmup;
	return 0;
}

static int
parse_config(const char *q_arg)
{
	char        s[256];
	const char *p, *p0 = q_arg;
	char       *end;
	enum fieldnames { FLD_PORT = 0, FLD_QUEUE, FLD_LCORE, _NUM_FLD };
	unsigned long int_fld[_NUM_FLD];
	char         *str_fld[_NUM_FLD];
	int           i;
	unsigned      size;

	nb_lcore_params = 0;

	while ((p = strchr(p0, '(')) != NULL) {
		++p;
		if ((p0 = strchr(p, ')')) == NULL)
			return -1;

		size = p0 - p;
		if (size >= sizeof(s))
			return -1;

		snprintf(s, sizeof(s), "%.*s", size, p);
		if (rte_strsplit(s, sizeof(s), str_fld, _NUM_FLD, ',') != _NUM_FLD)
			return -1;
		for (i = 0; i < _NUM_FLD; i++) {
			errno      = 0;
			int_fld[i] = strtoul(str_fld[i], &end, 0);
			if (errno != 0 || end == str_fld[i] || int_fld[i] > 255)
				return -1;
		}
		if (nb_lcore_params >= MAX_LCORE_PARAMS) {
			printf("exceeded max number of lcore params: %hu\n", nb_lcore_params);
			return -1;
		}
		lcore_params_array[nb_lcore_params].port_id  = (uint8_t)int_fld[FLD_PORT];
		lcore_params_array[nb_lcore_params].queue_id = (uint8_t)int_fld[FLD_QUEUE];

		lcore_params_array[nb_lcore_params].lcore_id = (uint8_t)int_fld[FLD_LCORE];
		++nb_lcore_params;
	}
	lcore_params = lcore_params_array;
	return 0;
}

static unsigned int
parse_queues(const char *q_arg)
{
	char         *end = NULL;
	unsigned long n;

	/* parse hexadecimal string */
	n = strtoul(q_arg, &end, 10);

	if ((q_arg[0] == '\0') || (end == NULL) || (*end != '\0'))
		return 0;
	if (n == 0)
		return 0;
	// if (n >= MAX_RX_QUEUE_PER_LCORE)
	// 	return 0;

	return n;
}

static int
parse_args(int argc, char **argv)
{
	int   opt, ret;
	char *prgname = argv[0];

	static const char short_options[] = "q:" /* queues       */
	                                    "d:" /* descriptors  */
	                                    "a"  /* aggressive   */
	                                    "v"  /* verbose      */
	                                    "s:" /* skip         */
	                                    "p:" /* portmask     */
	                                    "P"; /* promiscuous  */

	/* reset getopt internal state (important when relaunching app) */
	optind         = 1;
	char **argvopt = argv;

	while ((opt = getopt(argc, argvopt, short_options)) != EOF) {

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

			// case 'd':
			// 	nb_rxd = parse_queues(optarg);
			// 	if (nb_rxd < 64 || (nb_rxd & (nb_rxd - 1)) != 0) {
			// 		printf("invalid number of descriptors\n");
			// 		return -1;
			// 	}
			// 	printf("Number of descriptors per port: %u\n", nb_rxd);
			// 	break;

		case 'a':
			aggressive = true;
			break;

		case 'v':
			verbose = true;
			break;

		case 's':
			skip = atoi(optarg);
			break;

		default:
			printf("Wrong Param\n");
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
	// uint32_t magic = rte_cpu_to_be_32(magic_value);
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
	uint16_t                   nb_lcores = rte_lcore_count();
	unsigned                   lcore_id;
	static struct rte_mempool *mbuf_pool;

	setlocale(LC_NUMERIC, ""); // Usa locale di sistema per i separatori

	signal(SIGINT, signal_handler);
	signal(SIGTERM, signal_handler);

	int ret = rte_eal_init(argc, argv);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "EAL init failed\n");
	argc -= ret;
	argv += ret;

	/* parse application arguments (after the EAL ones) */
	ret = parse_args(argc, argv);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "Invalid MICA parameters\n");

	uint16_t nb_ports = rte_eth_dev_count_avail();
	if (nb_ports == 0)
		rte_exit(EXIT_FAILURE, "No available ports\n");

	/* ---- Mempool ---- */
	int nb_mbufs = RTE_MAX(
	    queues * 1 * (nb_rxd + nb_txd + MAX_PKT_BURST + nb_lcores * MEMPOOL_CACHE_SIZE), 8192U);
	mbuf_pool = rte_pktmbuf_pool_create(
	    "mbuf_pool", nb_mbufs, MEMPOOL_CACHE_SIZE, 0, RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());

	if (!mbuf_pool)
		rte_exit(EXIT_FAILURE, "Cannot create mbuf pool\n");

	for (int i = 0; i < 2048; i++) {
		queue_hit[i] = skip;
	}

	/* Configure port */
	struct rte_eth_conf port_conf = {0};
	port_conf.rxmode.mq_mode      = ETH_MQ_RX_NONE;
	uint16_t n_tx_queue           = 1;

	ret = rte_eth_dev_configure(portid, (uint16_t)queues, (uint16_t)n_tx_queue, &port_conf);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "Configure failed\n");

	// QueueDPDK setup
	// Check correct bitstream
	struct rte_eth_dev_info dev_info;
	ret = rte_eth_dev_info_get(portid, &dev_info);
	if (ret != 0)
		rte_exit(EXIT_FAILURE,
		         "Error during getting device (port %u) info: %s\n",
		         portid,
		         strerror(-ret));
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
	RTE_LCORE_FOREACH(lcore_id)
	{
		struct rte_eth_rxconf rxq_conf;

		rxq_conf          = dev_info.default_rxconf;
		rxq_conf.offloads = port_conf.rxmode.offloads;

		for (int x = 0; x < queues; x++) {
			int diag = rte_pmd_qdma_set_queue_mode(portid, x, RTE_PMD_QDMA_STREAMING_MODE);
			if (diag < 0)
				rte_exit(EXIT_FAILURE,
				         "rte_pmd_qdma_set_queue_mode : "
				         "Passing of STREAMING_MODE "
				         "failed\n");

			ret = rte_eth_rx_queue_setup(
			    portid, x, nb_rxd, rte_eth_dev_socket_id(portid), &rxq_conf, mbuf_pool);
			if (ret < 0)
				rte_exit(EXIT_FAILURE, "rte_eth_rx_queue_setup:err=%d, port=%u\n", ret, portid);
		}
	}

	/* TX queue */
	RTE_LCORE_FOREACH(lcore_id)
	{
		struct rte_eth_txconf *txconf;
		txconf            = &dev_info.default_txconf;
		txconf->offloads  = port_conf.txmode.offloads;
		uint16_t queue_id = rte_lcore_index(lcore_id);
		ret =
		    rte_eth_tx_queue_setup(portid, queue_id, nb_txd, rte_eth_dev_socket_id(portid), txconf);
		if (ret < 0)
			rte_exit(EXIT_FAILURE, "TX queue setup failed\n");
	}

	/* Start port */
	ret = rte_eth_dev_start(portid);
	if (ret < 0)
		rte_exit(EXIT_FAILURE, "Device start failed\n");

	printf("Port %u initialized.\n", portid);

#define RFC 1
	if (RFC) {
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
	}

	// mica init
	static uint64_t umem_size            = 4096;
	const size_t    page_size            = 1048576 * 2; // 2MB hugepages
	const size_t    num_numa_nodes       = 1;
	const size_t    num_pages_to_try     = umem_size;
	const size_t    num_pages_to_reserve = umem_size - umem_size / 8;
	size_t          alloc_overhead       = sizeof(struct mehcached_item);
	printf("a\n");

	mehcached_shm_init(page_size, num_numa_nodes, num_pages_to_try, num_pages_to_reserve);
	printf("aa\n");

	table               = &table_o;
	size_t numa_nodes[] = {(size_t)-1};
	// mehcached_table_init(table, 1, 1, 256, false, false, false, numa_nodes[0], numa_nodes,
	// MEHCACHED_MTH_THRESHOLD_FIFO);
	mehcached_table_init(table,
	                     (NUM_KEYS + MEHCACHED_ITEMS_PER_BUCKET - 1) / MEHCACHED_ITEMS_PER_BUCKET,
	                     1,
	                     NUM_KEYS * /*MEHCACHED_ROUNDUP64*/ (alloc_overhead + 8 + 8),
	                     false,
	                     false,
	                     false,
	                     numa_nodes[0],
	                     numa_nodes,
	                     MEHCACHED_MTH_THRESHOLD_FIFO);
	printf("aaa\n");

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
	rte_eal_mp_remote_launch(main_loop, NULL, CALL_MAIN);

	RTE_LCORE_FOREACH_WORKER(portid) { rte_eal_wait_lcore(portid); }

	mehcached_table_free(table);

	print_stats();

	printf("measured RX packets: %.2f\n", (float)measured_packets_rx);
	printf("measured RX Throughput: %.2f\n",
	       (double)measured_packets_rx / ((double)end_time / (double)rte_get_timer_hz()));
	printf("measured time: %.2f seconds\n", (double)end_time / (double)rte_get_timer_hz());

	return 0;
}
