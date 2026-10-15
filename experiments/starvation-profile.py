"""
TRex STL profile for the starvation / flow completion time tests.

Three kinds of streams, all with the packet size of zipf-profile.py:

* Zipf load: the same flows and rates as zipf-profile.py (same seed, same
  5-tuples). A log-spaced subset of ranks is "sampled": its payload carries
  'QB', the flow id, a per-packet seq written by the field engine and a shift.
  The starvation app logs a sampled packet when seq % 2^shift == 0, with the
  shift chosen so that each sampled flow logs about log_rate packets/s.
* Probes: TRex latency streams with the 5-tuple of selected Zipf ranks, so they
  share the queue of that flow. TRex appends a trailer with seq and TX
  timestamp, which the app logs for every packet.
* Finite flows (FCT): TRex latency streams with fresh 5-tuples sending bursts
  of N packets at fct_pps, one burst every ~N/fct_pkts_per_s seconds.

TRex does not scale latency streams with the port multiplier: only the Zipf
load does, and its base rate is num_flows pps, so mult = rate / num_flows.

build(**kwargs) returns (streams, meta); meta describes every marked flow for
experiments/starvation.py. register() keeps the file loadable by TRex.
"""

import importlib.util
import math
import os

import numpy as np
from trex.stl.api import (
    IP,
    UDP,
    Ether,
    Raw,
    STLFlowLatencyStats,
    STLPktBuilder,
    STLScVmRaw,
    STLStream,
    STLTXCont,
    STLTXMultiBurst,
    STLVmFlowVar,
    STLVmWrFlowVar,
)

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("zipf_profile", os.path.join(HERE, "zipf-profile.py"))
zipf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zipf)

SRC_MAC = "e8:eb:d3:78:95:8d"
DST_MAC = "58:a2:e1:d0:69:ce"
HDR_LEN = 42  # Ethernet + IPv4 + UDP
SEQ_OFFSET = HDR_LEN + 6
NO_LOG = 0xFF

PROBE_ID_BASE = 100000
FCT_ID_BASE = 200000

DEFAULTS = dict(
    num_flows=10000,
    skew=0.6,
    packet_size=64,
    ref_mpps=3.0,         # Zipf rate used to pick the log shifts
    logged_flows=200,     # sampled Zipf flows (log-spaced ranks)
    log_rate=500,         # logged packets/s per sampled flow at ref_mpps
    probe_ranks=(1, 3, 10, 30, 100, 300, 1000, 3000, 10000),
    probe_pps=500,
    fct_sizes=(1, 4, 16, 64, 256, 1024),
    fct_flows=4,          # finite flows per size
    fct_pps=200000,       # rate inside a burst
    fct_pkts_per_s=5000,  # average rate of every finite flow
    fct_max_bursts_s=200,
)


def _pkt(src, dst, sport, dport, size, head):
    base = Ether(src=SRC_MAC, dst=DST_MAC) / IP(src=src, dst=dst) / UDP(sport=sport, dport=dport)
    pad = max(0, size - HDR_LEN - len(head))
    return base / Raw(load=head + b"\x00" * pad)


def _head(magic, flow_id, shift=None):
    head = magic + flow_id.to_bytes(4, "big")
    if shift is not None:
        head += b"\x00\x00\x00\x00" + bytes([shift])
    return head


def _logged_ranks(num_flows, n):
    ranks = np.unique(np.round(np.logspace(0, math.log10(num_flows), n)).astype(int))
    return set(int(r) for r in ranks if 1 <= r <= num_flows)


def build(**kwargs):
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in kwargs.items() if k in DEFAULTS})
    n, size = cfg["num_flows"], cfg["packet_size"]
    if size < HDR_LEN + 2 + 4 + 16:
        raise ValueError("packets too small for the marker and the TRex trailer")

    zp = zipf.ZipfProfile()
    zp.num_flows, zp.skew = n, cfg["skew"]
    src_ips, dst_ips = zp._generate_ip_pools()
    probs = zp._generate_flow_weights()
    np.random.seed(42)
    sports = np.random.randint(1024, 65536, size=n)
    dports = np.random.randint(1024, 65536, size=n)

    logged = _logged_ranks(n, cfg["logged_flows"])
    ref_pps = cfg["ref_mpps"] * 1e6
    streams, meta = [], {"config": cfg, "flows": {}}

    # Zipf load: base rate num_flows pps in total, as zipf-profile.py
    for i in range(n):
        rank = i + 1
        tuple5 = (src_ips[i], dst_ips[i], int(sports[i]), int(dports[i]))
        base_pps = float(probs[i] * n)
        if rank in logged:
            shift = max(0, math.ceil(math.log2(max(1.0, probs[i] * ref_pps / cfg["log_rate"]))))
            pkt = _pkt(*tuple5, size, _head(b"QB", rank, shift))
            vm = STLScVmRaw([
                STLVmFlowVar(name="seq", min_value=0, max_value=0xFFFFFFFF, size=4, op="inc"),
                STLVmWrFlowVar(fv_name="seq", pkt_offset=SEQ_OFFSET),
            ])
            builder = STLPktBuilder(pkt=pkt, vm=vm)
            meta["flows"][rank] = dict(kind="bg", rank=rank, prob=float(probs[i]),
                                       shift=shift, tuple=tuple5)
        else:
            pkt = Ether(src=SRC_MAC, dst=DST_MAC) / IP(src=tuple5[0], dst=tuple5[1]) / \
                UDP(sport=tuple5[2], dport=tuple5[3]) / Raw(load=b"\x42" * (size - HDR_LEN))
            builder = STLPktBuilder(pkt=pkt)
        streams.append(STLStream(packet=builder, mode=STLTXCont(pps=base_pps)))

    pg_id = 1
    # Probes on the 5-tuple of selected Zipf ranks
    for k, rank in enumerate(r for r in cfg["probe_ranks"] if r <= n):
        i = rank - 1
        tuple5 = (src_ips[i], dst_ips[i], int(sports[i]), int(dports[i]))
        fid = PROBE_ID_BASE + k
        streams.append(STLStream(
            packet=STLPktBuilder(pkt=_pkt(*tuple5, size, _head(b"QL", fid))),
            mode=STLTXCont(pps=cfg["probe_pps"]),
            flow_stats=STLFlowLatencyStats(pg_id=pg_id),
        ))
        meta["flows"][fid] = dict(kind="probe", rank=rank, prob=float(probs[i]), pg_id=pg_id,
                                  pps=cfg["probe_pps"], tuple=tuple5)
        pg_id += 1

    # Finite flows with fresh 5-tuples (outside the zipf-profile.py pools)
    rng = np.random.RandomState(7)
    idx = 0
    for N in cfg["fct_sizes"]:
        bursts_s = min(cfg["fct_max_bursts_s"], cfg["fct_pkts_per_s"] / N)
        period_us = 1e6 / bursts_s
        burst_us = N / cfg["fct_pps"] * 1e6
        for j in range(cfg["fct_flows"]):
            tuple5 = (f"100.64.{idx // 250}.{idx % 250 + 1}", f"100.65.{j}.{N % 250 + 1}",
                      int(rng.randint(1024, 65536)), int(rng.randint(1024, 65536)))
            fid = FCT_ID_BASE + idx
            streams.append(STLStream(
                packet=STLPktBuilder(pkt=_pkt(*tuple5, size, _head(b"QL", fid))),
                isg=float(rng.uniform(0, period_us)),
                mode=STLTXMultiBurst(pps=cfg["fct_pps"], pkts_per_burst=N,
                                     ibg=max(1.0, period_us - burst_us), count=10 ** 6),
                flow_stats=STLFlowLatencyStats(pg_id=pg_id),
            ))
            meta["flows"][fid] = dict(kind="fct", size=N, pg_id=pg_id, pps=cfg["fct_pps"],
                                      period_us=period_us, tuple=tuple5)
            pg_id += 1
            idx += 1

    return streams, meta


class StarvationProfile(object):
    def get_streams(self, direction=0, **kwargs):
        return build(**kwargs)[0]


def register():
    return StarvationProfile()
