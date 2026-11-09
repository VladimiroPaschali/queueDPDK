"""
Reaction of the adaptive app (apps/adaptive/build/adaptive) to changing traffic.

A scenario is a sequence of phases; each phase sets the offered rate, the Zipf
skew, the number of flows and the packet size of the Zipf load of
zipf-profile.py. The app runs once for the whole scenario while TRex switches
phase. Rate and flow changes are applied on the fly: the streams of the
largest flow count of the scenario are loaded once, and a phase with N flows
keeps only the first N ranks running (pause_streams/resume_streams) and scales
the port so that they send the phase's rate. The first N ranks of a Zipf with
skew s are again a Zipf with skew s, and the flow sets are nested, so the
scenario never has more distinct flows than its largest phase (15000 fit in
the 16384 NAT entries; the NAT never expires a flow). A skew or size change
reloads the streams (the app then sees a short gap without traffic), as every
flow change does with --reload-flows, where each flow count is a different set
of 5-tuples. Every scenario is run with --adaptive and, for comparison, with a
fixed number of queues.

With --probe-pps (default 10000), TRex also sends --probes latency streams
(5-tuples outside the Zipf pools, each its own pg_id, so they land on several
queues), which TRex does not scale with the port multiplier. With
--flow-probe-pps (default 1000) it also sends the latency flow of latency.py,
whose queue the +latency policies poll every --latency-period polls: its
latency is reported apart (lq_*), in every run, so that every policy can be
compared on that flow. The NIC steers a packet to hash & (queues - 1), so the
flow's queue with N queues is its queue with 2048 queues (latency_queues.json)
& (N - 1), and the adaptive app follows it as the active queues change.

Every second the script samples TRex (offered and returned pps, loss, the
probes' latency histogram of that second) and the app's per-second statistics
(forwarded pps, empty polls, pkts/burst, active and polled queues), and logs
every queue change the controller makes.

Results in <output>/:
  runs.csv       one row per run
  logs/          the app's adaptive and RSS lines, with seconds since traffic start
  timeline.csv   one row per second: phase, TRex rates, probe latency (p50,
                 p99, max in us) and probe loss, the same for the latency
                 flow (lq_*), app statistics, queues
  decisions.csv  every queue change of the controller, with its cost
  snapshots.csv  with --snapshot-ms: packets waiting in every active queue at a
                 sweep start, the next 48 polls that returned packets
                 (queue:packets) and the empty polls skipped among them
  phases.csv     per run and phase: means over the whole phase and over its
                 second half (steady state), probe latency percentiles over
                 the phase and over its second half, queue count the
                 controller settles on and time to reach it
Plots: plot_adaptive.py.

Examples:
  python3 adaptive.py --scenarios ramp --static 128 --phase-duration 10
  python3 adaptive.py --variants "" "--adaptive-epoch 50" "--adaptive-threshold 1"
  python3 adaptive.py                         # every scenario, default comparison
"""

import argparse
import csv
import importlib.util
import json
import os
import re
import shlex
import subprocess as sp
import sys
import threading
import time
from collections import Counter
from time import sleep

import numpy as np
from tqdm import tqdm

sys.path.append("/trex/v3.08/automation/trex_control_plane/interactive")
from trex.stl.api import (  # noqa: E402
    IP,
    UDP,
    Ether,
    Raw,
    STLClient,
    STLFlowLatencyStats,
    STLPktBuilder,
    STLProfile,
    STLStream,
    STLTXCont,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ndz = _load("no_drop_zip", "no-drop-zip.py")
lat = _load("latency", "latency.py")  # TRex latency histogram helpers

TREX_SERVER = "100.78.72.16"
PORTS = [0]
PROFILE_FILE = os.path.join(HERE, "zipf-profile.py")
APP = (ndz.NF_CHAIN.format(lcores="0")
       .replace("(0,0,0),(0,1,1)", "(0,0,0)")
       .replace("/nf-chain/build/nf-chain", "/experiments/apps/adaptive/build/adaptive"))
APP_PATTERN = "experiments/apps/adaptive/build/adaptive"
# --app mica: the single-core MICA of latency.py built with the controller
# (mica/Makefile, -fifo-nocopy-prefetch-adaptive), same output lines
APPS = {
    "nf-chain": (APP, APP_PATTERN),
    "mica": (lat.APPS["mica-prefetch"].replace("/mica/build/mica-fifo-nocopy-prefetch-lat ",
                                               "/mica/build/mica-fifo-nocopy-prefetch-adaptive "),
             "mica/build/mica-fifo-nocopy-prefetch-adaptive"),
}
assert "-adaptive " in APPS["mica"][0]
POLICIES = ["baseline", "aggressive", "penalty", "aggressive+penalty", "aggressive+latency"]
MAX_FLOWS = 20000  # zipf-profile.py caps the flows here

# Latency probes: 5-tuples outside the address pools of zipf-profile.py (and
# of the finite flows of starvation-profile.py), MACs of zipf-profile.py
PROBE_SRC, PROBE_DST, PROBE_SPORT, PROBE_DPORT = "100.66.0.1", "100.67.0.1", 20000, 8902
PROBE_MACS = dict(src="e8:eb:d3:78:95:8d", dst="58:a2:e1:d0:69:ce")
PROBE_PG_BASE = 100


def phase(rate, skew=0.6, flows=10000, size=64):
    """rate in Mpps (0 stops the traffic)."""
    return dict(rate=rate, skew=skew, flows=flows, size=size)


SCENARIOS = {
    # NF chain, flows changing at 3.5 Mpps: from 10000 flows on above the
    # zero-loss rate of every fixed queue count (at most ~3.5 Mpps, Aggressive
    # on 128 queues), and of 1 queue already from 5000 flows (~3.0)
    "flows-rate35": [phase(3.5, flows=f) for f in (100, 1000, 5000, 10000, 15000, 5000, 1000, 100)],
    # load staircase up and down, with finer steps around the single-core
    # capacity (~2.4-3.4 Mpps)
    "ramp": [phase(r) for r in (0.5, 1, 1.5, 2, 2.5, 3, 3.25, 3.5, 3.25, 3, 2.5, 2, 1, 0.5)],
    # MICA (--app mica) forwards about twice nf-chain's rate on a core: the
    # flows and ramp scenarios at twice the rates
    "mica-flows-rate7": [phase(7.0, flows=f) for f in (100, 1000, 5000, 10000, 15000, 5000, 1000, 100)],
    "mica-ramp": [phase(2 * r) for r in (0.5, 1, 1.5, 2, 2.5, 3, 3.25, 3.5, 3.25, 3, 2.5, 2, 1, 0.5)],
    # the MICA of the paper (header prefetch and AVX-512 tag comparison, mica/Makefile), about 1 Mpps faster per core than
    # the 7 Mpps scenarios above assume (those are for a build with MICA_HT=0): up to 8 and 9 Mpps, above what 128 fixed
    # queues take (NDR 7.9 at skew 0.6) and within reach of 512 to 1024 (8.8, 9.1)
    "mica-flows-rate8": [phase(8.0, flows=f) for f in (100, 1000, 5000, 10000, 15000, 5000, 1000, 100)],
    "mica-ramp-max9": [phase(r) for r in (3, 4, 5, 6, 7, 8, 8.5, 9, 8.5, 8, 7, 6, 5, 4, 3)],
}

NUM = r"([\d.,']+)"
RE_RX = re.compile(r"Total Packets received:\s*" + NUM + r"\s*\(diff:\s*" + NUM + r"\)")
RE_EMPTY = re.compile(r"empty/sec \(the poll read no packets\):\s*(\d+)")
RE_PPB = re.compile(r"pkts/burst \(resets every second\):\s*([\d.]+)")
RE_RSS = re.compile(r"RSS queues: (\d+) \(polled (\d+)\)(?:, adaptive (\w+), (\d+) changes)?")
RE_DECISION = re.compile(r"adaptive: (\d+) -> (\d+) queues after (\w+) epoch: ([\d.]+) cycles/pkt "
                         r"\(base ([\d.]+)\)")
RE_LOCATE = re.compile(r"RSS mask register: (0x[0-9a-f]+)")
RE_SNAPSHOT = re.compile(r"snapshot: tsc_ms=([\d.]+) queues=(\d+) backlog=(\S*) polls=(\S*)(?: empty=(\d+))?")
RE_MIX = re.compile(r"poll mix: active polls (\d+), empty ([\d.]+)%, full ([\d.]+)%, busy ([\d.]+)"
                    r"(?:, backlog max ([\d.]+)%, over half ([\d.]+)%)?")


def to_int(s):
    return int(re.sub(r"\D", "", s))


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def launch_app(args):
    """Start the app; every output line is kept with its arrival time."""
    command = f"{APP} {args}"
    tqdm.write("Running: " + command)
    proc = sp.Popen(shlex.split(command), stdout=sp.PIPE, stderr=sp.STDOUT, text=True, bufsize=1)
    ready = threading.Event()
    lines = []

    def reader():
        for line in proc.stdout:
            lines.append((time.monotonic(), line))
            if "ready" in line.lower() or "entering main loop" in line.lower():
                ready.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    proc.lines, proc.reader = lines, thread
    if not ready.wait(timeout=120):
        stop_app(proc)
        raise RuntimeError("adaptive app did not become ready:\n" + "".join(l for _, l in lines[-30:]))
    return proc


def stop_app(proc):
    proc.terminate()
    try:
        proc.wait(timeout=60)
    except sp.TimeoutExpired:
        sp.run(["sudo", "pkill", "-9", "-f", APP_PATTERN])
        proc.wait()
    proc.reader.join(timeout=10)
    return proc.lines


def parse_app(lines, t0):
    """Per-second statistics blocks and controller decisions, times relative to t0."""
    stats, decisions, cur = [], [], {}
    for t, line in lines:
        if m := RE_RX.search(line):
            # the app prints the per-second difference unsigned: a counter
            # that went back shows up as ~2^64
            rx = to_int(m.group(2))
            cur = dict(t=t - t0, app_rx_pps=rx if rx < 1e9 else "")
        elif m := RE_EMPTY.search(line):
            cur["empty_per_s"] = int(m.group(1))
        elif m := RE_PPB.search(line):
            cur["pkts_per_burst"] = float(m.group(1))
        elif m := RE_MIX.search(line):
            cur.update(empty_pct=float(m.group(2)), full_pct=float(m.group(3)), busy=float(m.group(4)),
                       backlog_max_pct=float(m.group(5) or 0), backlog_half_pct=float(m.group(6) or 0))
        elif (m := RE_RSS.search(line)) and "t" in cur:
            cur.update(queues=int(m.group(1)), polled=int(m.group(2)),
                       ctl_phase=m.group(3) or "", changes=int(m.group(4) or 0))
            stats.append(cur)
            cur = {}
        elif m := RE_DECISION.search(line):
            decisions.append(dict(t=t - t0, from_q=int(m.group(1)), to_q=int(m.group(2)),
                                  after=m.group(3), cost=float(m.group(4)), base_cost=float(m.group(5))))
    return stats, decisions


# ---------------------------------------------------------------------------
# TRex
# ---------------------------------------------------------------------------

class Traffic:
    """Zipf streams and latency probes on the TRex port.

    The Zipf streams are built once per (skew, flows, size) and kept. With
    runtime_flows the flows of a phase are the first ranks of the largest flow
    count of the scenario (superset): a flow change pauses or resumes the
    ranks above the phase's count and scales the port, so the active streams
    send the phase's rate and the traffic never stops. Otherwise, and on a
    skew or size change, the streams are reloaded."""

    def __init__(self, client, start_spread_us=0, probes=0, probe_pps=0, runtime_flows=True, flow_probe_pps=0):
        self.client = client
        self.start_spread_us = start_spread_us
        self.probes = probes if probe_pps > 0 else 0
        self.probe_pps = probe_pps
        self.flow_probe_pps = flow_probe_pps
        self.runtime_flows = runtime_flows
        self.cache = {}
        self.cum_pps = {}  # cumulative base pps of the Zipf streams, by rank
        self.loaded = None
        self.running = False
        self.ids = []      # TRex ids of the loaded Zipf streams, by rank
        self.active = 0    # Zipf streams not paused
        self.start_n = 0   # Zipf streams not flagged start_paused

    @property
    def pg_ids(self):
        return [PROBE_PG_BASE + k for k in range(self.probes)]

    @property
    def flow_pg_ids(self):
        return [lat.PROBE_PG_ID] if self.flow_probe_pps > 0 else []

    def key(self, ph, superset):
        flows = superset if self.runtime_flows else ph["flows"]
        return (ph["skew"], min(flows, MAX_FLOWS), ph["size"])

    def streams(self, key):
        if key not in self.cache:
            streams = STLProfile.load_py(PROFILE_FILE, direction=0, port_id=0, skew=key[0],
                                         num_flows=key[1], packet_size=key[2],
                                         start_spread_us=self.start_spread_us).get_streams()
            self.cache[key] = streams
            self.cum_pps[key] = np.cumsum([s.fields["mode"]["rate"]["value"] for s in streams])
        return self.cache[key]

    def probe_streams(self, size):
        """One latency stream per probe 5-tuple, each with its own pg_id, and
        the latency flow of latency.py."""
        streams = [STLStream(packet=STLPktBuilder(pkt=lat.probe_packet()), mode=STLTXCont(pps=self.flow_probe_pps),
                             flow_stats=STLFlowLatencyStats(pg_id=pg_id)) for pg_id in self.flow_pg_ids]
        for k, pg_id in enumerate(self.pg_ids):
            pkt = (Ether(**PROBE_MACS) / IP(src=PROBE_SRC, dst=PROBE_DST)
                   / UDP(sport=PROBE_SPORT + k, dport=PROBE_DPORT))
            pkt = pkt / Raw(load=b"\x42" * max(0, size - len(pkt)))
            streams.append(STLStream(packet=STLPktBuilder(pkt=pkt), mode=STLTXCont(pps=self.probe_pps / self.probes),
                                     flow_stats=STLFlowLatencyStats(pg_id=pg_id)))
        return streams

    def stop(self):
        self.client.stop(ports=PORTS)
        self.running = False

    def apply(self, ph, superset):
        """Switch to phase ph, superset being the largest flow count of the
        scenario; returns the seconds spent reloading streams."""
        if ph["rate"] <= 0:
            self.stop()
            return 0.0
        key = self.key(ph, superset)
        streams = self.streams(key)
        n = min(ph["flows"], key[1])
        # a plain factor: TRex scales every stream's base pps, not the probes
        mult = f"{ph['rate'] * 1e6 / self.cum_pps[key][n - 1]}"
        if key == self.loaded and self.running:
            if n > self.active:
                # lower the rate first, so the resumed flows never overshoot it
                self.client.update(ports=PORTS, mult=mult, force=True)
                self.client.resume_streams(port=PORTS[0], stream_ids=self.ids[self.active:n])
            else:
                if n < self.active:
                    self.client.pause_streams(port=PORTS[0], stream_ids=self.ids[n:self.active])
                self.client.update(ports=PORTS, mult=mult, force=True)
            self.active = n
            return 0.0
        t = time.monotonic()
        self.load(ph, superset)
        self.client.start(ports=PORTS, mult=mult, force=True)
        self.running = True
        return time.monotonic() - t

    def load(self, ph, superset):
        """Put the streams of phase ph on the (stopped) port without starting them. Loading takes 3-5 s the first
        time a stream set is added: run_scenario does it before the app starts, so that the first apply() of
        every run only starts the traffic and the phases of the runs begin at the same time."""
        key = self.key(ph, superset)
        streams = self.streams(key)
        n = min(ph["flows"], key[1])
        # a restart keeps the start_paused flags of the load, not the pauses since
        if key != self.loaded or not (n == self.start_n == self.active):
            self.stop()
            self.client.reset(ports=PORTS)
            for i, s in enumerate(streams):
                s.fields["start_paused"] = i >= n
            ids = self.client.add_streams(streams + self.probe_streams(key[2]), ports=PORTS)
            self.ids, self.loaded, self.start_n, self.active = ids[:len(streams)], key, n, n


def read_probes(client, pg_ids):
    """Cumulative latency histogram (us bucket -> packets) and packets sent and
    received of all the probes."""
    if not pg_ids:
        return None
    pg = client.get_pgid_stats(pg_ids)
    hist, tx, rx = Counter(), 0.0, 0.0
    for pg_id in pg_ids:
        h = lat.pg_entry(pg.get("latency", {}), pg_id).get("latency", {}).get("histogram", {})
        for lo, n in h.items():
            hist[float(lo)] += int(n)
        f = lat.pg_entry(pg.get("flow_stats", {}), pg_id)
        tx += lat.counter_total(f.get("tx_pkts", {}))
        rx += lat.counter_total(f.get("rx_pkts", {}))
    return dict(hist=hist, tx=tx, rx=rx)


def probe_delta(cur, last):
    """What the probes did between two readings; a reload resets the counters."""
    if cur is None:
        return None
    if last is None or cur["tx"] < last["tx"] or any(n < last["hist"].get(lo, 0) for lo, n in cur["hist"].items()):
        return cur
    return dict(hist=Counter({lo: n - last["hist"].get(lo, 0) for lo, n in cur["hist"].items()}),
                tx=cur["tx"] - last["tx"], rx=cur["rx"] - last["rx"])


def latency_fields(deltas, prefix="lat_"):
    """p50, p99 and max latency (us) and probe loss of the summed deltas."""
    hist, tx, rx = Counter(), 0.0, 0.0
    for d in deltas:
        if d:
            hist.update(d["hist"])
            tx += d["tx"]
            rx += d["rx"]
    pct = lat.histogram_percentiles({lo: n for lo, n in hist.items() if n > 0})
    out = {f"{prefix}{k}": round(pct[p], 1) if pct else "" for k, p in (("p50", 50), ("p99", 99), ("max", "max"))}
    out[f"{prefix}probe_loss_pct"] = round(max(0.0, (tx - rx) / tx * 100), 3) if tx > 0 else ""
    return out


def latency_flow_queue(queues):
    """Queue of the latency flow of latency.py with this many queues."""
    with open(lat.QUEUE_CACHE) as f:
        known = json.load(f)
    key = f"{lat.PROBE['src']}:{lat.PROBE['sport']}->{lat.PROBE['dst']}:{lat.PROBE['dport']}/udp"
    top = max(int(q) for q in known[key])
    if queues > top:
        raise RuntimeError(f"the latency flow's queue is known up to {top} queues ({lat.QUEUE_CACHE})")
    return known[key][str(top)] & (queues - 1)


def policy_args(policy, queues):
    """--policy of the app, plus --latency-queue for the +latency policies."""
    if policy.endswith("+latency"):
        return f"--policy {policy.removesuffix('+latency')} --latency-queue {latency_flow_queue(queues)}"
    return f"--policy {policy}"


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

def run_scenario(client, traffic, args, scenario, policy, mode, variant, rep, writer):
    key = dict(scenario=scenario, policy=policy, mode=mode, variant=variant, rep=rep)
    if mode == "adaptive":
        app_args = f"-q {args.max_queues} {policy_args(policy, args.max_queues)} --adaptive {variant}"
    else:
        queues = int(mode.split('-')[1])
        app_args = f"-q {queues} {policy_args(policy, queues)}"
    if args.snapshot_ms:
        app_args += f" --snapshot-ms {args.snapshot_ms}"
    phases = SCENARIOS[scenario]
    # the largest flow count per stream set (skew, size) of the scenario
    superset = Counter()
    for ph in phases:
        superset[ph["skew"], ph["size"]] = max(superset[ph["skew"], ph["size"]], ph["flows"])

    def flows_of(ph):
        return superset[ph["skew"], ph["size"]]

    # build every stream set before the app starts
    for ph in phases:
        if ph["rate"] > 0:
            traffic.streams(traffic.key(ph, flows_of(ph)))
    traffic.stop()
    if phases[0]["rate"] > 0:
        traffic.load(phases[0], flows_of(phases[0]))

    proc = launch_app(app_args)
    samples, bounds = [], []
    try:
        sleep(args.idle)  # the app starts with no traffic
        t0 = time.monotonic()
        last = last_probes = last_flow_probe = None
        for i, ph in enumerate(phases):
            reload_s = traffic.apply(ph, flows_of(ph))
            start = time.monotonic() - t0
            bounds.append(dict(phase=i, start=start, reload_s=reload_s, **ph))
            end = start + args.phase_duration
            while True:
                now = time.monotonic() - t0
                if now >= end:
                    break
                sleep(min(1.0, end - now))
                st = client.get_stats(ports=PORTS)[PORTS[0]]
                probes = read_probes(client, traffic.pg_ids)
                flow_probe = read_probes(client, traffic.flow_pg_ids)
                t = time.monotonic() - t0
                # TRex's tx_pps/rx_pps are smoothed over a few seconds: rates
                # come from the counters, TRex's only for the first sample and
                # after a stream reload (which resets the counters)
                row = dict(t=t, phase=i, tx_pps=st["tx_pps"], rx_pps=st["rx_pps"],
                           opackets=st["opackets"], ipackets=st["ipackets"])
                if last is not None:
                    dt = t - last["t"]
                    dtx = row["opackets"] - last["opackets"]
                    drx = row["ipackets"] - last["ipackets"]
                    if dt > 0 and dtx >= 0 and drx >= 0:
                        row["tx_pps"], row["rx_pps"] = dtx / dt, drx / dt
                        row["loss_pct"] = max(0.0, (dtx - drx) / dtx * 100) if dtx > 0 else 0.0
                # the probes' latency in this second (the first reading: since the load)
                row["probes"] = probe_delta(probes, last_probes)
                row["flow_probe"] = probe_delta(flow_probe, last_flow_probe)
                row.update(latency_fields([row["probes"]]))
                row.update(latency_fields([row["flow_probe"]], "lq_"))
                last, last_probes, last_flow_probe = row, probes, flow_probe
                samples.append(row)
        traffic.stop()
        sleep(1.0)
    finally:
        traffic.stop()
        lines = stop_app(proc)

    out = "".join(l for _, l in lines)
    logdir = os.path.join(writer.dir, "logs")
    os.makedirs(logdir, exist_ok=True)
    with open(os.path.join(logdir, f"{scenario}_{policy}_{mode}{('_' + variant.replace(' ', '')) if variant else ''}"
                                   f"_r{rep}.log"), "w") as f:
        f.writelines(f"{t - t0:9.3f} {l}" for t, l in lines if "adaptive:" in l or "RSS" in l)
    if mode == "adaptive" and not RE_LOCATE.search(out):
        raise RuntimeError("the app did not find the RSS mask register:\n" + out[-3000:])
    stats, decisions = parse_app(lines, t0)

    # queue snapshots: packets waiting per queue, then the next polls (queue:packets)
    snapshots = []
    for t, line in lines:
        if m := RE_SNAPSHOT.search(line):
            ts = t - t0
            ph = max((b for b in bounds if b["start"] <= ts), key=lambda b: b["start"], default=None)
            if ph is None:
                continue
            snapshots.append(key | dict(t=round(ts, 3), phase=ph["phase"], offered_mpps=ph["rate"], skew=ph["skew"],
                                        flows=ph["flows"], queues=int(m.group(2)), backlog=m.group(3).rstrip(","),
                                        polls=m.group(4).rstrip(","), empty_polls=int(m.group(5) or 0)))

    # timeline: every TRex sample with the latest app statistics
    timeline, j = [], 0
    for s in samples:
        while j + 1 < len(stats) and stats[j + 1]["t"] <= s["t"]:
            j += 1
        app = stats[j] if stats and stats[j]["t"] <= s["t"] else {}
        ph = phases[s["phase"]]
        timeline.append(key | dict(t=round(s["t"], 3), phase=s["phase"], offered_mpps=ph["rate"], skew=ph["skew"],
                                   flows=ph["flows"], size=ph["size"], tx_pps=s["tx_pps"], rx_pps=s["rx_pps"],
                                   loss_pct=s.get("loss_pct", ""),
                                   lat_p50=s["lat_p50"], lat_p99=s["lat_p99"], lat_max=s["lat_max"],
                                   probe_loss_pct=s["lat_probe_loss_pct"],
                                   lq_p50=s["lq_p50"], lq_p99=s["lq_p99"], lq_max=s["lq_max"],
                                   lq_probe_loss_pct=s["lq_probe_loss_pct"],
                                   app_rx_pps=app.get("app_rx_pps", ""), empty_per_s=app.get("empty_per_s", ""),
                                   pkts_per_burst=app.get("pkts_per_burst", ""), queues=app.get("queues", ""),
                                   empty_pct=app.get("empty_pct", ""), full_pct=app.get("full_pct", ""),
                                   busy=app.get("busy", ""),
                                   backlog_max_pct=app.get("backlog_max_pct", ""),
                                   backlog_half_pct=app.get("backlog_half_pct", ""),
                                   polled=app.get("polled", ""), ctl_phase=app.get("ctl_phase", "")))

    # per phase: whole phase and second half (steady state)
    phase_rows = []
    for b in bounds:
        rows = [r for r in timeline if r["phase"] == b["phase"]]
        if not rows:
            continue
        mid = b["start"] + args.phase_duration / 2
        steady = [r for r in rows if r["t"] >= mid] or rows
        in_samples = [x for x in samples if x["phase"] == b["phase"]]
        steady_samples = [x for x in in_samples if x["t"] >= mid] or in_samples
        lat_phase = (latency_fields([x["probes"] for x in in_samples])
                     | latency_fields([x["probes"] for x in steady_samples], "steady_lat_")
                     | latency_fields([x["flow_probe"] for x in in_samples], "lq_")
                     | latency_fields([x["flow_probe"] for x in steady_samples], "steady_lq_"))

        def mean(rs, k):
            v = [r[k] for r in rs if r[k] != ""]
            return sum(v) / len(v) if v else ""

        qs = [r["queues"] for r in steady if r["queues"] != ""]
        settled = Counter(qs).most_common(1)[0][0] if qs else ""
        reach = next((r["t"] - b["start"] for r in rows if r["queues"] == settled), "")
        in_phase = [d for d in decisions if b["start"] <= d["t"] < b["start"] + args.phase_duration]
        phase_rows.append(key | dict(
            phase=b["phase"], rate=b["rate"], skew=b["skew"], flows=b["flows"], size=b["size"],
            reload_s=round(b["reload_s"], 2),
            rx_pps=mean(rows, "rx_pps"), steady_rx_pps=mean(steady, "rx_pps"),
            loss_pct=mean(rows, "loss_pct"), steady_loss_pct=mean(steady, "loss_pct"),
            steady_empty_per_s=mean(steady, "empty_per_s"), steady_pkts_per_burst=mean(steady, "pkts_per_burst"),
            steady_empty_pct=mean(steady, "empty_pct"), steady_full_pct=mean(steady, "full_pct"),
            steady_busy=mean(steady, "busy"), steady_queues_mean=mean(steady, "queues"),
            steady_backlog_max_pct=mean(steady, "backlog_max_pct"),
            **lat_phase,
            settled_queues=settled, time_to_settle_s=round(reach, 2) if reach != "" else "",
            changes=len(in_phase), queues_seen=" ".join(str(q) for q in sorted(set(qs)))))

    run = key | dict(
        app_args=app_args, phases=len(phases), phase_duration=args.phase_duration,
        rx_pps=sum(r["rx_pps"] for r in timeline) / max(1, len(timeline)),
        **latency_fields([x["probes"] for x in samples]),
        **latency_fields([x["flow_probe"] for x in samples], "lq_"),
        changes=len(decisions), samples=len(timeline), app_stats=len(stats))
    writer.write("runs.csv", [run])
    writer.write("timeline.csv", timeline)
    writer.write("decisions.csv", [key | d for d in decisions])
    writer.write("phases.csv", phase_rows)
    writer.write("snapshots.csv", snapshots)
    return run, phase_rows


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class Writer:
    def __init__(self, directory):
        self.dir = directory
        self.files = {}

    def write(self, name, rows):
        if not rows:
            return
        path = os.path.join(self.dir, name)
        new = name not in self.files and not os.path.exists(path)
        fields = self.files.get(name)
        if fields is None:
            if new:
                fields = list(dict.fromkeys(k for r in rows for k in r))
            else:
                with open(path) as f:
                    fields = next(csv.reader(f))
            self.files[name] = fields
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(rows)


def done_runs(directory):
    path = os.path.join(directory, "runs.csv")
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return {(r["scenario"], r["policy"], r["mode"], r["variant"], int(r["rep"])) for r in csv.DictReader(f)}


def print_comparison(rows):
    """Steady throughput per phase: adaptive against the fixed queue counts."""
    by_phase = {}
    for r in rows:
        name = r["mode"] if r["mode"] != "adaptive" else f"adaptive{(' ' + r['variant']) if r['variant'] else ''}"
        by_phase.setdefault(r["phase"], {})[name] = r
    for p, runs in sorted(by_phase.items()):
        any_row = next(iter(runs.values()))
        desc = f"phase {p}: {any_row['rate']} Mpps skew {any_row['skew']} {any_row['flows']} flows {any_row['size']}B"
        parts = []
        for name, r in runs.items():
            rx = r["steady_rx_pps"]
            s = f"{name} {rx / 1e6:.2f}" if rx != "" else f"{name} -"
            if r.get("steady_busy", "") != "":
                s += f" busy {r['steady_busy']:.2f}"
            if r.get("steady_lat_p99", "") != "":
                s += f" p99 {r['steady_lat_p99']:.0f}us"
            if r.get("steady_lq_p99", "") != "":
                s += f" (latency flow {r['steady_lq_p99']:.0f}us)"
            if name.startswith("adaptive"):
                s += f" (q={r['settled_queues']} in {r['time_to_settle_s']}s, {r['changes']} changes)"
            parts.append(s)
        tqdm.write(f"  {desc}: " + ", ".join(parts) + " Mpps")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", default="nf-chain", choices=list(APPS),
                        help="mica: mica-fifo-nocopy-prefetch-adaptive (use the mica-* scenarios)")
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    parser.add_argument("--policies", nargs="+", default=["aggressive+penalty"], choices=POLICIES)
    parser.add_argument("--static", type=int, nargs="*", default=[1, 128, 2048],
                        help="fixed queue counts run for comparison (none: only adaptive)")
    parser.add_argument("--variants", nargs="+", default=[""],
                        help="extra adaptive options, one run each, e.g. \"--adaptive-epoch 50\"; a single "
                             "option needs a leading space (\" --adaptive-search\"), or argparse takes it as "
                             "an option of this script")
    parser.add_argument("--max-queues", type=int, default=2048, help="-q of the adaptive runs")
    parser.add_argument("--phase-duration", type=float, default=20.0, help="seconds per phase")
    parser.add_argument("--idle", type=float, default=2.0, help="seconds without traffic after startup")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--snapshot-ms", type=int, default=0,
                        help="queue snapshots of the app every this many ms (snapshots.csv), 0 = none")
    parser.add_argument("--start-spread-us", type=float, default=0.0,
                        help="spread the start of the flows over this many us (zipf-profile.py), so that "
                             "a stream reload does not send the first packet of every flow at once")
    parser.add_argument("--probe-pps", type=float, default=10000,
                        help="total rate of the latency probes, 0 = none")
    parser.add_argument("--probes", type=int, default=32, help="latency probe 5-tuples (one pg_id each)")
    parser.add_argument("--flow-probe-pps", type=float, default=1000,
                        help="rate of the latency flow of latency.py (its queue is the --latency-queue of the "
                             "+latency policies), 0 = none")
    parser.add_argument("--reload-flows", action="store_true",
                        help="reload the streams on every flow change (a new set of 5-tuples per flow count) "
                             "instead of pausing and resuming the ranks of the largest set")
    parser.add_argument("--output", default="results/adaptive/runs", help="relative to experiments/")
    args = parser.parse_args()
    global APP, APP_PATTERN
    APP, APP_PATTERN = APPS[args.app]
    args.variants = [v.strip() for v in args.variants]

    out_dir = os.path.join(HERE, args.output)
    os.makedirs(out_dir, exist_ok=True)
    writer = Writer(out_dir)
    done = done_runs(out_dir)

    modes = [("adaptive", v) for v in args.variants] + [(f"static-{q}", "") for q in args.static]
    runs = [(s, p, m, v, r) for s in args.scenarios for p in args.policies for m, v in modes
            for r in range(args.repetitions)]
    todo = [x for x in runs if x not in done]
    traffic_s = sum(len(SCENARIOS[s]) * args.phase_duration + args.idle for s, *_ in todo)
    tqdm.write(f"{len(todo)} runs to do ({len(runs) - len(todo)} already in {out_dir}), "
               f"~{traffic_s / 60:.0f} min of traffic")

    client = STLClient(server=TREX_SERVER)
    client.connect()
    client.acquire(ports=PORTS, force=True)
    client.reset(ports=PORTS)
    ndz.set_governor("performance")
    traffic = Traffic(client, args.start_spread_us, args.probes, args.probe_pps, not args.reload_flows,
                      args.flow_probe_pps)
    try:
        pbar = tqdm(total=len(todo), unit="run")
        group = None
        group_rows = []
        for scenario, policy, mode, variant, rep in todo:
            if group != (scenario, policy, rep):
                if group_rows:
                    print_comparison(group_rows)
                group, group_rows = (scenario, policy, rep), []
                tqdm.write(f"\n=== {scenario} | {policy} | rep {rep}")
            pbar.set_description(f"{scenario} {policy} {mode} {variant}".strip())
            for attempt in range(2):
                try:
                    run, phase_rows = run_scenario(client, traffic, args, scenario, policy, mode, variant,
                                                   rep, writer)
                    group_rows += phase_rows
                    tqdm.write(f"  {mode} {variant}: {run['rx_pps'] / 1e6:.2f} Mpps mean, "
                               f"{run['changes']} queue changes, probe p99 {run['lat_p99']} us")
                    break
                except Exception as e:  # noqa: BLE001
                    tqdm.write(f"⚠️ run failed ({e}); {'retrying' if attempt == 0 else 'skipped'}")
                    sp.run(["sudo", "pkill", "-9", "-f", APP_PATTERN])
                    traffic.loaded = None
                    sleep(3)
            pbar.update(1)
        if group_rows:
            print_comparison(group_rows)
        pbar.close()
    finally:
        ndz.release_trex(client)
        ndz.set_governor("schedutil")
    tqdm.write(f"Results in {out_dir}/")


if __name__ == "__main__":
    t = time.time()
    main()
    tqdm.write(f"elapsed {time.time() - t:.0f}s")
