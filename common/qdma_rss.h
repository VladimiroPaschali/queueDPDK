/* SPDX-License-Identifier: BSD-3-Clause */
#pragma once

#include <stdint.h>

/* User-BAR offset of the RSS mask (active queues - 1) of an ethdev port */
uint32_t qdma_rss_mask_reg(uint16_t port_id);

/* Packets waiting in an RX queue (O(1), from the completion indexes) */
uint16_t qdma_rx_backlog(uint16_t port_id, uint16_t queue_id);

/* Descriptors of an RX queue */
uint16_t qdma_rx_ring_size(uint16_t port_id, uint16_t queue_id);
