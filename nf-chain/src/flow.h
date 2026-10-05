#pragma once
#include <stddef.h>
#include <stdint.h>

struct FlowId {
  uint16_t src_port;
  uint16_t dst_port;
  uint32_t src_ip;
  uint32_t dst_ip;
  uint16_t internal_device;
  uint8_t protocol;
};

/* The Shared variant stored FlowIds without the trailing padding byte. Field
 * offsets are the same, only the per-entry stride of the flow vector changes. */
#define FLOWID_PACKED_SIZE (offsetof(struct FlowId, protocol) + sizeof(uint8_t))
