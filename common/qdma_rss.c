/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * Address of the Open-NIC RSS mask register, computed like the patched QDMA
 * PMD does in qdma_dev_reta_update(): the first indirection-table entry of the
 * PCI function the PMD read from the hardware (qdma_pci_dev.func_id). Kept in
 * its own file because the PMD's private headers clash with main.c.
 */
#include <rte_ethdev.h>

#include "qdma.h"
#include "qdma_soft_reg.h"
#include "qdma_rss.h"

uint32_t
qdma_rss_mask_reg(uint16_t port_id)
{
	const struct qdma_pci_dev *qdma_dev = rte_eth_devices[port_id].data->dev_private;

	return QDMA_FUNC_OFFSET_INDIR_TABLE(qdma_dev->func_id, 0);
}

/*
 * Completions the hardware wrote to an RX queue and the driver has not read
 * yet: the packets waiting in the ring. Computed like rx_queue_count() in the
 * PMD, from the write-back producer index and the completion consumer index,
 * but without walking the entries (O(1)).
 */
uint16_t
qdma_rx_backlog(uint16_t port_id, uint16_t queue_id)
{
	const struct qdma_rx_queue *rxq  = rte_eth_devices[port_id].data->rx_queues[queue_id];
	const uint16_t              tail = rxq->cmpt_cidx_info.wrb_cidx;
	const uint16_t              pidx = rxq->wb_status->pidx;

	return tail <= pidx ? pidx - tail : rxq->nb_rx_cmpt_desc - 1 - tail + pidx;
}

/* Descriptors of an RX queue */
uint16_t
qdma_rx_ring_size(uint16_t port_id, uint16_t queue_id)
{
	const struct qdma_rx_queue *rxq = rte_eth_devices[port_id].data->rx_queues[queue_id];

	return rxq->nb_rx_desc;
}
