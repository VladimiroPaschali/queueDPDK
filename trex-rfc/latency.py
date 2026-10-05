"""
Latency of nf-chain (paper Fig. 6) with TRex.

TRex sends the Zipf load of zipf-profile.py at a fixed rate plus a probe flow
with a 5-tuple of its own, carrying TRex latency stats. For every queue count,
policy and rate the script reports the probe's p50/p90/p95/p99 and the load's
loss, so points that are not zero-loss can be told apart.

The Latency policy needs the queue the probe lands on. The Open-NIC RSS picks
hash & (queues - 1) with a hardware hash, so instead of recomputing it the
script measures it: it runs nf-chain on its own, sends only the probe and reads
the per-queue histogram the application prints on exit. Results are cached in
latency_queues.json.

MICA is not supported: it writes a 1400-byte value into the packet payload,
which overwrites the latency header TRex appends to the probe.

Examples:
  python3 latency.py --queues 1 64 --min 1 --max 5 --step 0.5
  python3 latency.py --policies baseline latency+aggressive --skew 0.9
"""

import argparse
import csv
import importlib.util
import json
import math
import os
import re
import shlex
import statistics
import subprocess as sp
import sys
import threading
from time import sleep

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

# Shared settings of the zero-loss runs (TRex server, nf-chain command line)
_spec = importlib.util.spec_from_file_location("no_drop_zip", os.path.join(HERE, "no-drop-zip.py"))
ndz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ndz)

TREX_SERVER = "100.78.72.16"
PORTS = [0]
PROFILE_FILE = os.path.join(HERE, "zipf-profile.py")
NF_CHAIN = ndz.EXPERIMENTS["nf-chain"]["base_command"]
QUEUE_CACHE = os.path.join(HERE, "latency_queues.json")

# Probe flow: outside the address pools zipf-profile.py draws from
PROBE = {"src": "10.10.10.10", "dst": "10.20.20.20", "sport": 1234, "dport": 8902}
PROBE_PG_ID = 7
PACKET_SIZE = 64

# --policy and latency options for each tested configuration
POLICIES = {
    "baseline": "--policy baseline",
    "aggressive": "--policy aggressive",
    "latency": "--policy baseline --latency-queue {q}",
    "latency+aggressive": "--policy aggressive --latency-queue {q}",
}

PERCENTILES = (50, 90, 95, 99)
FIELDS = [
    "queues", "policy", "latency_queue", "rate_mpps", "repetition",
    "tx_pps", "rx_pps", "loss_pct", "zero_loss",
    "probe_tx", "probe_rx", "min", "avg", "p50", "p90", "p95", "p99", "max",
]
METRICS = ["tx_pps", "rx_pps", "loss_pct", "min", "avg", "p50", "p90", "p95", "p99", "max"]


# ---------------------------------------------------------------------------
# TRex streams
# ---------------------------------------------------------------------------

def probe_packet():
    pkt = (
        Ether(src="e8:eb:d3:78:95:8d", dst="58:a2:e1:d0:69:ce")
        / IP(src=PROBE["src"], dst=PROBE["dst"])
        / UDP(sport=PROBE["sport"], dport=PROBE["dport"])
    )
    return pkt / Raw(load=b"\x42" * max(0, PACKET_SIZE - len(pkt)))


def load_streams(client, skew, flows, probe_pps):
    """Zipf load plus the latency probe.

    zipf-profile.py gives every flow a base rate summing to @flows pps and the
    port is scaled with start(mult=...). TRex does not apply the multiplier to
    latency streams, so the probe keeps its absolute rate @probe_pps.
    """
    client.stop(ports=PORTS)
    client.reset(ports=PORTS)
    streams = STLProfile.load_py(
        PROFILE_FILE, direction=0, port_id=0,
        num_flows=flows, skew=skew, packet_size=PACKET_SIZE,
    ).get_streams()
    streams.append(STLStream(
        packet=STLPktBuilder(pkt=probe_packet()),
        mode=STLTXCont(pps=probe_pps),
        flow_stats=STLFlowLatencyStats(pg_id=PROBE_PG_ID),
    ))
    client.add_streams(streams, ports=PORTS)


def probe_only_stream(client):
    client.stop(ports=PORTS)
    client.reset(ports=PORTS)
    client.add_streams(
        STLStream(packet=STLPktBuilder(pkt=probe_packet()), mode=STLTXCont(pps=10000)),
        ports=PORTS,
    )


# ---------------------------------------------------------------------------
# nf-chain process
# ---------------------------------------------------------------------------

def launch_app(args):
    command = f"{NF_CHAIN} {args}"
    tqdm.write("Running: " + command)
    proc = sp.Popen(shlex.split(command), stdout=sp.PIPE, stderr=sp.STDOUT, text=True, bufsize=1)
    ready = threading.Event()
    lines = []

    def reader():
        for line in proc.stdout:
            lines.append(line)
            if "ready" in line.lower() or "entering main loop" in line.lower():
                ready.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    proc.lines, proc.reader = lines, thread
    if not ready.wait(timeout=60):
        stop_app(proc)
        raise RuntimeError("nf-chain did not become ready:\n" + "".join(lines[-20:]))
    return proc


def stop_app(proc):
    proc.terminate()
    proc.wait()
    proc.reader.join(timeout=10)
    return "".join(proc.lines)


# ---------------------------------------------------------------------------
# Latency queue discovery
# ---------------------------------------------------------------------------

def parse_queue_histogram(output):
    """{queue: packets} from nf-chain's 'queue=  N -> count (pct%)' lines."""
    hist = {}
    for m in re.finditer(r"queue=\s*(\d+)\s*->\s*([\d.,']+)", output):
        hist[int(m.group(1))] = int(re.sub(r"\D", "", m.group(2)))
    return hist


def discover_latency_queue(client, queues):
    proc = launch_app(f"-q {queues} --policy baseline")
    try:
        client.clear_stats(ports=PORTS)
        client.start(ports=PORTS, force=True, duration=3)
        client.wait_on_traffic(ports=PORTS)
        sleep(1)
    finally:
        output = stop_app(proc)

    hist = parse_queue_histogram(output)
    total = sum(hist.values())
    if total == 0:
        raise RuntimeError(f"No probe packet reached nf-chain with {queues} queues")
    queue, count = max(hist.items(), key=lambda kv: kv[1])
    share = count / total
    tqdm.write(f"  {queues} queues: probe on queue {queue} ({share:.1%} of {total} packets)")
    if share < 0.9:
        raise RuntimeError(f"Probe spread over several queues with {queues} queues: {hist}")
    return queue


def latency_queues(client, queue_counts, rediscover):
    """Queue the probe lands on for each queue count, cached across runs."""
    key = f"{PROBE['src']}:{PROBE['sport']}->{PROBE['dst']}:{PROBE['dport']}/udp"
    cache = {}
    if os.path.isfile(QUEUE_CACHE):
        with open(QUEUE_CACHE) as f:
            cache = json.load(f)
    known = {} if rediscover else cache.get(key, {})

    missing = [q for q in queue_counts if str(q) not in known]
    if missing:
        tqdm.write(f"Discovering the probe queue for {missing} queues...")
        probe_only_stream(client)
        for q in missing:
            known[str(q)] = 0 if q == 1 else discover_latency_queue(client, q)
        cache[key] = known
        with open(QUEUE_CACHE, "w") as f:
            json.dump(cache, f, indent=2, sort_keys=True)

    # RSS takes hash & (queues - 1): the queue for fewer queues is the low bits
    largest = str(max(int(q) for q in known))
    for q, queue in known.items():
        if known[largest] & (int(q) - 1) != queue:
            tqdm.write(f"⚠️ probe queue {queue} with {q} queues is not "
                       f"{known[largest]} & {int(q) - 1}: RSS is not hash & mask")
    return {q: known[str(q)] for q in queue_counts}


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def bucket_upper(lo):
    """Upper bound of a TRex latency bucket, keyed by its lower bound (see
    XDP_CLONE/microbenchmark/bench_common.py): 0 covers [0, 10), and the step
    at every other magnitude is the magnitude itself."""
    if lo <= 0:
        return 10.0
    return lo + 10 ** (len(str(int(lo))) - 1)


def histogram_percentiles(histogram):
    buckets = sorted((float(lo), int(n)) for lo, n in histogram.items() if int(n) > 0)
    if not buckets:
        return None
    total = sum(n for _, n in buckets)
    out = {}
    for p in PERCENTILES:
        target = p / 100.0 * total
        cumulative = 0
        out[p] = bucket_upper(buckets[-1][0])
        for lo, n in buckets:
            previous, cumulative = cumulative, cumulative + n
            if cumulative >= target:
                fraction = min(1.0, max(0.0, (target - previous) / n))
                out[p] = lo + fraction * (bucket_upper(lo) - lo)
                break
    out["max"] = bucket_upper(buckets[-1][0])
    return out


def pg_entry(section, pg_id):
    return section.get(pg_id, section.get(str(pg_id), {}))


def counter_total(values):
    if "total" in values:
        return float(values["total"])
    return float(sum(v for k, v in values.items() if str(k).isdigit()))


def measure(client, rate_mpps, warmup, duration):
    client.stop(ports=PORTS)
    sleep(1)  # let the queues drain between rates
    client.start(ports=PORTS, mult=f"{rate_mpps}mpps", force=True)
    sleep(warmup)

    client.clear_stats(ports=PORTS)
    client.clear_pgid_stats(clear_flow_stats=True, clear_latency_stats=True)
    sleep(duration)
    port = client.get_stats(ports=PORTS)[PORTS[0]]
    pg = client.get_pgid_stats([PROBE_PG_ID])

    latency = pg_entry(pg.get("latency", {}), PROBE_PG_ID).get("latency", {})
    flow = pg_entry(pg.get("flow_stats", {}), PROBE_PG_ID)
    tx, rx = float(port["opackets"]), float(port["ipackets"])
    loss = max(0.0, (tx - rx) / tx * 100) if tx else float("nan")

    result = {
        "tx_pps": tx / duration,
        "rx_pps": rx / duration,
        "loss_pct": loss,
        "zero_loss": loss <= 0.1,  # same tolerance as the NDR search (pdr=0.1)
        "probe_tx": counter_total(flow.get("tx_pkts", {})),
        "probe_rx": counter_total(flow.get("rx_pkts", {})),
        "min": float(latency.get("total_min", "nan")),
        "avg": float(latency.get("average", "nan")),
    }
    pct = histogram_percentiles(latency.get("histogram", {}))
    for p in PERCENTILES:
        result[f"p{p}"] = pct[p] if pct else float("nan")
    result["max"] = pct["max"] if pct else float("nan")
    return result


def write_results(path, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path, rows):
    groups = {}
    for row in rows:
        groups.setdefault((row["queues"], row["policy"], row["rate_mpps"]), []).append(row)
    fieldnames = ["queues", "policy", "latency_queue", "rate_mpps", "repetitions", "zero_loss"]
    for m in METRICS:
        fieldnames += [f"{m}_mean", f"{m}_stdev"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for (queues, policy, rate), group in groups.items():
            out = {
                "queues": queues, "policy": policy, "rate_mpps": rate,
                "latency_queue": group[0]["latency_queue"],
                "repetitions": len(group),
                "zero_loss": all(r["zero_loss"] for r in group),
            }
            for m in METRICS:
                values = [r[m] for r in group if not math.isnan(r[m])]
                out[f"{m}_mean"] = statistics.fmean(values) if values else float("nan")
                out[f"{m}_stdev"] = statistics.stdev(values) if len(values) > 1 else 0.0
            writer.writerow(out)


def rate_range(lo, hi, step):
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 3) for i in range(n + 1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queues", type=int, nargs="+", default=ndz.ALL_QUEUES)
    parser.add_argument("--policies", nargs="+", default=list(POLICIES), choices=list(POLICIES))
    parser.add_argument("--min", type=float, default=1.0, help="lowest rate in Mpps")
    parser.add_argument("--max", type=float, default=5.0, help="highest rate in Mpps")
    parser.add_argument("--step", type=float, default=0.5, help="rate step in Mpps")
    parser.add_argument("--skew", type=float, default=0.6, help="Zipf skew of the load")
    parser.add_argument("--flows", type=int, default=10000)
    parser.add_argument("--probe-pps", type=int, default=1000, help="rate of the latency probe")
    parser.add_argument("--warmup", type=float, default=2.0, help="seconds before each sample")
    parser.add_argument("--duration", type=float, default=5.0, help="seconds per sample")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--rediscover", action="store_true", help="ignore latency_queues.json")
    args = parser.parse_args()

    rates = rate_range(args.min, args.max, args.step)
    results_dir = os.path.join(HERE, f"results_latency_nf-chain_skew{args.skew}")
    os.makedirs(results_dir, exist_ok=True)
    results_csv = os.path.join(results_dir, "latency_results.csv")
    summary_csv = os.path.join(results_dir, "latency_summary.csv")

    client = STLClient(server=TREX_SERVER)
    client.connect()
    client.acquire(ports=PORTS, force=True)
    ndz.set_governor("performance")
    rows = []
    try:
        lat_queue = latency_queues(client, args.queues, args.rediscover)
        load_streams(client, args.skew, args.flows, args.probe_pps)

        total = len(args.queues) * len(args.policies) * len(rates) * args.repetitions
        pbar = tqdm(total=total, unit="pt")
        for queues in args.queues:
            for policy in args.policies:
                app_args = f"-q {queues} " + POLICIES[policy].format(q=lat_queue[queues])
                proc = launch_app(app_args)
                try:
                    for rate in rates:
                        for rep in range(1, args.repetitions + 1):
                            pbar.set_description(f"Q={queues:4d} {policy:18} {rate:.2f} Mpps")
                            r = measure(client, rate, args.warmup, args.duration)
                            r.update(queues=queues, policy=policy, latency_queue=lat_queue[queues],
                                     rate_mpps=rate, repetition=rep)
                            rows.append(r)
                            write_results(results_csv, rows)
                            tqdm.write(
                                f"Q={queues} {policy} {rate} Mpps: p50={r['p50']:.1f} "
                                f"p99={r['p99']:.1f} us, loss={r['loss_pct']:.3f}% "
                                f"probe {r['probe_rx']:.0f}/{r['probe_tx']:.0f}"
                            )
                            pbar.update(1)
                finally:
                    client.stop(ports=PORTS)
                    stop_app(proc)
        pbar.close()
    finally:
        if rows:
            write_summary(summary_csv, rows)
        ndz.release_trex(client)
        ndz.set_governor("schedutil")
    tqdm.write(f"Results in {results_dir}/")


if __name__ == "__main__":
    main()
