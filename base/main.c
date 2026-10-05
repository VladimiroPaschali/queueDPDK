/* SPDX-License-Identifier: BSD-3-Clause
 * Copyright(c) 2010-2016 Intel Corporation
 */

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
#include <unistd.h>

#include <rte_common.h>
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

#define MAX_PKT_BURST 32
#define MEMPOOL_CACHE_SIZE 256
#define RTE_TEST_RX_DESC_DEFAULT 1024
#define RTE_TEST_TX_DESC_DEFAULT 1024

static volatile bool force_quit = false;
static uint16_t portid = 0;
static uint16_t nb_rxd = RTE_TEST_RX_DESC_DEFAULT;
static uint16_t nb_txd = RTE_TEST_TX_DESC_DEFAULT;

static struct rte_mempool *mbuf_pool;

static void
signal_handler(int signum)
{
	if (signum == SIGINT || signum == SIGTERM) {
		printf("\n\nSignal %d received, preparing to exit...\n", signum);
		force_quit = true;
	}
}

static int
port_init(uint16_t port, struct rte_mempool *mbuf_pool)
{
	struct rte_eth_conf port_conf = {0};
	struct rte_eth_txconf txconf;
	struct rte_eth_rxconf rxconf;
	struct rte_eth_dev_info dev_info;
	int retval;
	uint16_t q;
	uint16_t nb_lcores;

	if (!rte_eth_dev_is_valid_port(port)) {
		printf("Port %u is not valid\n", port);
		return -1;
	}

	nb_lcores = rte_lcore_count();

	printf("Initializing port %u with %u RX queues and %u TX queues...\n",
	       port, nb_lcores, nb_lcores);

	retval = rte_eth_dev_configure(port, nb_lcores, nb_lcores, &port_conf);
	if (retval != 0) {
		printf("rte_eth_dev_configure failed: %s\n", rte_strerror(-retval));
		return retval;
	}

	// QueueDPDK setup
	// Check correct bitstream
	int ret = rte_eth_dev_info_get(portid, &dev_info);
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
	printf("reta-queues %u\n", reta_conf[0].reta[0] + 1);

	// End QueueDPDK setup

	retval = rte_eth_dev_adjust_nb_rx_tx_desc(port, &nb_rxd, &nb_txd);
	if (retval != 0) {
		printf("rte_eth_dev_adjust_nb_rx_tx_desc failed: %s\n", rte_strerror(-retval));
		return retval;
	}

	rte_eth_dev_info_get(port, &dev_info);
	rxconf = dev_info.default_rxconf;
	for (q = 0; q < nb_lcores; q++) {
		retval = rte_eth_rx_queue_setup(port, q, nb_rxd, 
		                                 rte_eth_dev_socket_id(port),
		                                 &rxconf, mbuf_pool);
		if (retval < 0) {
			printf("rte_eth_rx_queue_setup failed: %s\n", rte_strerror(-retval));
			return retval;
		}
	}

	rte_eth_dev_info_get(port, &dev_info);
	txconf = dev_info.default_txconf;
	for (q = 0; q < nb_lcores; q++) {
		retval = rte_eth_tx_queue_setup(port, q, nb_txd,
		                                 rte_eth_dev_socket_id(port),
		                                 &txconf);
		if (retval < 0) {
			printf("rte_eth_tx_queue_setup failed: %s\n", rte_strerror(-retval));
			return retval;
		}
	}

	retval = rte_eth_dev_start(port);
	if (retval < 0) {
		printf("rte_eth_dev_start failed: %s\n", rte_strerror(-retval));
		return retval;
	}


	printf("Port %u started successfully\n", port);
	return 0;
}

static int
lcore_main(void *arg)
{
	(void)arg;
	uint16_t lcore_id = rte_lcore_id();
	uint16_t nb_lcores = rte_lcore_count();
	uint16_t queue_id = lcore_id % nb_lcores;
	struct rte_mbuf *pkts_burst[MAX_PKT_BURST];
	uint16_t nb_rx, nb_tx;

	printf("Starting lcore %u (queue %u)\n", lcore_id, queue_id);

	while (!force_quit) {
		nb_rx = rte_eth_rx_burst(portid, queue_id, pkts_burst, MAX_PKT_BURST);

		if (nb_rx > 0) {
			nb_tx =rte_eth_tx_burst(portid, queue_id, pkts_burst, nb_rx);
		}
		if (nb_tx < nb_rx) {
			printf("core %d err\n", lcore_id);
			for (uint16_t i = nb_tx; i < nb_rx; i++) {
				rte_pktmbuf_free(pkts_burst[i]);
			}
		}
	}

	printf("Stopping lcore %u\n", lcore_id);
	return 0;
}

int
main(int argc, char *argv[])
{
	int ret;
	unsigned lcore_id;

	ret = rte_eal_init(argc, argv);
	if (ret < 0) {
		rte_exit(EXIT_FAILURE, "Error with EAL initialization\n");
	}

	signal(SIGINT, signal_handler);
	signal(SIGTERM, signal_handler);

	uint16_t nb_lcores = rte_lcore_count();
	printf("Number of lcores: %u\n", nb_lcores);

	if (nb_lcores < 1) {
		rte_exit(EXIT_FAILURE, "No lcores available\n");
	}

	mbuf_pool = rte_pktmbuf_pool_create("MBUF_POOL",
	                                     nb_lcores * 4096,
	                                     MEMPOOL_CACHE_SIZE,
	                                     0,
	                                     RTE_MBUF_DEFAULT_BUF_SIZE,
	                                     rte_socket_id());

	if (mbuf_pool == NULL) {
		rte_exit(EXIT_FAILURE, "Cannot create mbuf pool\n");
	}

	if (port_init(portid, mbuf_pool) != 0) {
		rte_exit(EXIT_FAILURE, "Cannot init port %u\n", portid);
	}

	RTE_LCORE_FOREACH_WORKER(lcore_id) {
		rte_eal_remote_launch(lcore_main, NULL, lcore_id);
	}

	lcore_main(NULL);

	rte_eal_mp_wait_lcore();

	rte_eth_dev_stop(portid);
	rte_eth_dev_close(portid);

	rte_mempool_free(mbuf_pool);

	printf("Application terminated gracefully\n");
	return 0;
}
