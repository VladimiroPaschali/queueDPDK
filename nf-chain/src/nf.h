#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <signal.h>

#include <rte_common.h>
#include <rte_eal.h>
#include <rte_errno.h>
#include <rte_ethdev.h>
#include <rte_lcore.h>
#include <rte_mbuf.h>
#include <rte_malloc.h>
#include <rte_ether.h>
#include <rte_ip.h>

#include "nat_main.h"
#include "nf-log.h"
#include "nf-util.h"
#include "utils/boilerplate-util.h"
#include "utils/packet-io.h"
#include "utils/vigor-time.h"
#include "utils/tcpudp_hdr.h"




#define FLOOD_FRAME ((uint16_t)-1)

struct nf_config;

// #ifdef WITH_DPDK
// int nf_process(uint16_t device, uint8_t *buffer, uint16_t packet_length,vigor_time_t now);
// #elif defined(WITH_XCHG)
// int nf_process(uint16_t device, uint8_t *buffer, uint16_t packet_length,vigor_time_t now);
// #endif

/* Allocate nb_shards NAT flow tables. shared selects the Shared variant
 * (locked tables, 15-byte FlowIds); drop_full drops packets of new flows
 * when a shard is full instead of forwarding them untranslated. */
bool nf_init(uint16_t nb_shards, bool shared, bool drop_full);
struct FlowManager *nf_shard(uint16_t shard);
extern struct nf_config config;
void nf_config_init(int argc, char **argv);
void nf_config_usage(void);
void nf_config_print(void);

void worker_main(void);

#ifdef KLEE_VERIFICATION
void nf_loop_iteration_border(unsigned lcore_id, vigor_time_t time);
#endif
