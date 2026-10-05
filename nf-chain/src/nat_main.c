#include "nat_main.h"
#include <stdio.h>
#include <limits.h>
#include <arpa/inet.h>

struct nf_config config;

/*
 * The flow table is split into shards, each owning a disjoint range of
 * external ports: one shard on a single core, one per lcore with Partitioned,
 * one per RX queue with Shared.
 */
static struct FlowManager **nat_shards;
static uint16_t nat_shard_count;
static bool drop_when_full;

bool nf_init(uint16_t nb_shards, bool shared, bool drop_full) {

    // Setups the configuration
    config.start_port = 1024;
    // config.max_flows = 1024;
    config.max_flows = 16384;
    config.expiration_time = 60 * 1000000; // 60 seconds
    config.device_macs =
        calloc(rte_eth_dev_count_avail(), sizeof(struct rte_ether_addr));
    config.endpoint_macs =
        calloc(rte_eth_dev_count_avail(), sizeof(struct rte_ether_addr));
    for (uint8_t i = 0; i < rte_eth_dev_count_avail(); i++) {
        config.device_macs[i].addr_bytes[0] = 0x00;
        config.device_macs[i].addr_bytes[1] = 0x00;
        config.device_macs[i].addr_bytes[2] = 0x00;
        config.device_macs[i].addr_bytes[3] = 0x00;
        config.device_macs[i].addr_bytes[4] = 0x00;
        config.device_macs[i].addr_bytes[5] = i;

        config.endpoint_macs[i].addr_bytes[0] = 0x00;
        config.endpoint_macs[i].addr_bytes[1] = 0x00;
        config.endpoint_macs[i].addr_bytes[2] = 0x00;
        config.endpoint_macs[i].addr_bytes[3] = 0x00;
        config.endpoint_macs[i].addr_bytes[4] = 0x00;
        config.endpoint_macs[i].addr_bytes[5] = i;
    }
    config.external_addr = 1 << 24 | 1 << 16 | 1 << 8 | 1;
    config.wan_device = 2;
    config.lan_main_device = 0;

    if (nb_shards == 0 || nb_shards > config.max_flows) {
        return false;
    }
    if ((uint32_t)config.start_port + config.max_flows > (uint32_t)UINT16_MAX + 1U) {
        return false;
    }

    nat_shards = calloc(nb_shards, sizeof(*nat_shards));
    if (nat_shards == NULL) {
        return false;
    }
    nat_shard_count = nb_shards;
    drop_when_full = drop_full;

    const uint32_t base_flows = config.max_flows / nb_shards;
    const uint32_t remainder = config.max_flows % nb_shards;
    uint32_t shard_start = config.start_port;

    for (uint16_t shard = 0; shard < nb_shards; shard++) {
        const uint32_t shard_flows = base_flows + (shard < remainder ? 1U : 0U);
        nat_shards[shard] = flow_manager_allocate(
            (uint16_t)shard_start, config.external_addr, config.wan_device,
            config.expiration_time, shard_flows, shared);
        if (nat_shards[shard] == NULL) {
            return false;
        }
        shard_start += shard_flows;
    }

    return true;
}

struct FlowManager *nf_shard(uint16_t shard) {
    return shard < nat_shard_count ? nat_shards[shard] : NULL;
}

void uint32_to_ipv4(uint32_t ip, char *buffer) {
    struct in_addr addr;
    addr.s_addr = htonl(ip);
    // Convert the binary IP address to a string
    const char *result = inet_ntop(AF_INET, &addr, buffer, INET_ADDRSTRLEN);
    if (result == NULL) {
        perror("inet_ntop");
    }
}
int nf_process(struct FlowManager *flow_manager, uint16_t device,
               uint8_t *payload, uint16_t ether_type, uint8_t ip_proto,
               uint32_t ip_src, uint32_t ip_dst, uint16_t port_src,
               uint16_t port_dst, vigor_time_t now) {
    //flow_manager_expire(flow_manager, now);

#ifdef ENABLE_LOG
    char ip_src_str[256];
    char ip_dst_str[256];
    uint32_to_ipv4(ip_src, ip_src_str);
    uint32_to_ipv4(ip_dst, ip_dst_str);
    // Convert the binary IP address to a string
    NF_DEBUG("Flows have been expired");
    NF_DEBUG("Ether-type %u", ether_type);
    NF_DEBUG("IP protocol %u", ip_proto);
    NF_DEBUG("IP source %s", ip_src_str);
    NF_DEBUG("IP destination %s", ip_dst_str);
    NF_DEBUG("Port source %u", htons(port_src));
    NF_DEBUG("Port destination %u", htons(port_dst));
#endif
    // Do simple pointer arithmetic to prepare headers
    struct rte_ether_hdr *rte_ether_header = (struct rte_ether_hdr *)payload;
    struct rte_ipv4_hdr *rte_ipv4_header =
        (struct rte_ipv4_hdr *)(payload + sizeof(struct rte_ether_hdr));
    struct tcpudp_hdr *tcpudp_header =
        (struct tcpudp_hdr *)(payload + sizeof(struct rte_ether_hdr) +
                              sizeof(struct rte_ipv4_hdr));

    // Check ethertype
    if (ether_type != 0x0800) {
        NF_DEBUG("Not IPv4, dropping");
        // printf("Not IPv4, dropping\n");
        // return EXPLICIT_DROP;
    }

    // Check IP protocol
    if (ip_proto != 0x06 && ip_proto != 0x11) {
        NF_DEBUG("Not TCP/UDP, dropping");
        // printf("Not TCP/UDP, dropping\n");
        // return EXPLICIT_DROP;
    }

    uint16_t dst_device;
    if (device == config.wan_device) {
        NF_DEBUG("Device %" PRIu16 " is external", device);

        struct FlowId internal_flow;
        if (flow_manager_get_external(flow_manager, port_dst, now,
                                      &internal_flow)) {
            NF_DEBUG("Found internal flow.");

            if (internal_flow.dst_ip != ip_src ||
                internal_flow.dst_port != port_dst ||
                internal_flow.protocol != ip_proto) {

                NF_DEBUG("Spoofing attempt, dropping.");
                // printf("spoofing attempt, dropping\n");
                // return EXPLICIT_DROP;
            }

            // This time we have no choice, we have to manually go into the
            // payload and change the IP and port
            rte_ipv4_header->dst_addr = internal_flow.src_ip;
            tcpudp_header->dst_port = internal_flow.src_port;
            dst_device = internal_flow.internal_device;
        } else {
            NF_DEBUG("Unknown flow, dropping");
            // printf("unknown flow, dropping\n");
            // return EXPLICIT_DROP;
        }
    } else {
        struct FlowId id = {.src_port = port_src,
                            .dst_port = port_dst,
                            .src_ip = ip_src,
                            .dst_ip = ip_dst,
                            .protocol = ip_proto,
                            .internal_device = device};
        NF_DEBUG("For id:");

        NF_DEBUG("Device %" PRIu16 " is internal (not %" PRIu16 ")", device,
                 config.wan_device);

        uint16_t external_port;
        if (!flow_manager_get_internal(flow_manager, &id, now,
                                       &external_port)) {
            NF_DEBUG("New flow");

            if (!flow_manager_allocate_flow(flow_manager, &id, device, now,
                                            &external_port)) {
                NF_DEBUG("No space for the flow");
                if (drop_when_full)
                    return EXPLICIT_DROP;
            }
        }
        NF_DEBUG("Forwarding from ext port:%d", external_port);
        // printf("Forwarding from ext port:%d\n", external_port);
        rte_ipv4_header->src_addr = config.external_addr;
        tcpudp_header->src_port = external_port;
        dst_device = config.wan_device;
    }
    // concretize_devices(&dst_device, 2);
    return dst_device;
}
