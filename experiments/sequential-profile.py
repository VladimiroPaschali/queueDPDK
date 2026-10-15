"""
TRex STL profile for the throughput bound of a perfect queue-selection policy:
the flows of zipf-profile.py (same seed, same 5-tuples, same Zipf weights) sent
one after the other instead of interleaved. Flow 1 sends its whole train, then
flow 2, and so on; after the last flow the cycle starts again. Every burst the
app reads then holds a single flow, the best locality any policy could obtain.

The train of every flow is proportional to its Zipf weight, so the packet mix
is the one of the normal test and only the order changes; the coldest flow
sends min_train packets (default 32, a full burst), the others
round(min_train * p_i / p_min).

The trains are single-burst streams chained with `next`, looping from the last
back to the first. TRex splits a burst over its data-path cores, which would
interleave the flows again: start the traffic on one core (core_mask), as
no-drop-zip.py does for the experiments that use this profile. The port
multiplier scales the rate of every train, BASE_PPS each.
"""

import importlib.util
import os

import numpy as np
from trex.stl.api import IP, UDP, Ether, Raw, STLPktBuilder, STLStream, STLTXSingleBurst

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("zipf_profile", os.path.join(HERE, "zipf-profile.py"))
zipf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zipf)

SRC_MAC = "e8:eb:d3:78:95:8d"
DST_MAC = "58:a2:e1:d0:69:ce"
BASE_PPS = 1.0


def trains(probs, min_train):
    return np.maximum(1, np.round(min_train * probs / probs.min())).astype(int)


class SequentialProfile(object):
    def get_streams(self, direction=0, **kwargs):
        zp = zipf.ZipfProfile()
        zp.num_flows = min(kwargs.get("num_flows", 10000), 20000)
        zp.skew = kwargs.get("skew", 0.6)
        size = kwargs.get("packet_size", 64)
        min_train = kwargs.get("min_train", 32)

        src_ips, dst_ips = zp._generate_ip_pools()
        probs = zp._generate_flow_weights()
        np.random.seed(42)  # the ports of zipf-profile.py
        src_ports = np.random.randint(1024, 65536, size=zp.num_flows)
        dst_ports = np.random.randint(1024, 65536, size=zp.num_flows)
        counts = trains(probs, min_train)

        streams = []
        n = zp.num_flows
        for i in range(n):
            pkt = (Ether(src=SRC_MAC, dst=DST_MAC) / IP(src=src_ips[i], dst=dst_ips[i]) /
                   UDP(sport=int(src_ports[i]), dport=int(dst_ports[i])) / Raw(load=b"\x42" * max(0, size - 42)))
            streams.append(STLStream(
                name=f"f{i}",
                packet=STLPktBuilder(pkt=pkt),
                mode=STLTXSingleBurst(total_pkts=int(counts[i]), pps=BASE_PPS),
                next=f"f{(i + 1) % n}",
                self_start=(i == 0),
            ))
        print(f"[SequentialProfile] {n} flows, skew {zp.skew}: trains of {counts.min()}-{counts.max()} "
              f"packets, {counts.sum():,} packets per cycle")
        return streams


def register():
    return SequentialProfile()
