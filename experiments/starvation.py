"""
Starvation and flow completion time (FCT) campaign for nf-chain.

Runs starvation/build/starvation (nf-chain plus the qprobe log) under the
traffic of starvation-profile.py and, for every queue count, policy, skew and
offered rate:

* drain gap per queue: time between the last poll that left a queue empty and
  the next poll that returned packets (upper bound of the RX ring wait);
* probes (TRex latency streams on the 5-tuple of selected Zipf ranks): one-way
  delay relative to the minimum, computed from the TRex TX timestamp and the
  host TSC after a linear fit of the two clocks, plus loss and reordering from
  the TRex seq; the TRex round-trip latency of the same streams is kept too;
* finite flows: FCT and slowdown per burst of N packets;
* sampled Zipf flows: loss and reordering from the field-engine seq, and a
  delay estimate from the seq (TX time ~ seq / rate). The estimate includes
  the TX jitter of the TRex cores (stalls of ~1.5 ms were seen on one core),
  so use the probes for delays and these flows for loss and reordering.

The app is restarted for every point, so every point has its own log.

Results in <output>/: runs.csv, queues.csv, flows.csv, fct.csv,
delayhist.csv, drainhist.csv (plots: plot_starvation.py).

Examples:
  python3 starvation.py --queues 64 --policies aggressive --skews 0.6 --rates 2 --duration 4
  python3 starvation.py                       # the full campaign
  python3 starvation.py --analyze LOG.bin     # re-analyse a kept log
"""

import argparse
import csv
import importlib.util
import os
import re
import shlex
import subprocess as sp
import sys
import threading
import time
from time import sleep

import numpy as np
from tqdm import tqdm

sys.path.append("/trex/v3.08/automation/trex_control_plane/interactive")
from trex.stl.api import STLClient  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lat = _load("latency", "latency.py")  # TRex helpers of the latency campaign
ndz = lat.ndz
profile = _load("starvation_profile", "starvation-profile.py")

TREX_SERVER = "100.78.72.16"
PORTS = [0]
APP = (ndz.NF_CHAIN.format(lcores="0")
       .replace("(0,0,0),(0,1,1)", "(0,0,0)")
       .replace("/nf-chain/build/nf-chain", "/starvation/build/starvation"))
POLICIES = ["baseline", "aggressive", "penalty", "aggressive+penalty"]
RX_DESC = 1024

REC_DTYPE = np.dtype([
    ("tsc", "<u8"), ("trex_ts", "<u8"), ("flow_id", "<u4"), ("seq", "<u4"),
    ("queue", "<u2"), ("kind", "u1"), ("pos", "u1"), ("nb_rx", "<u2"), ("lcore", "<u2"),
])
KIND_BG, KIND_LAT, KIND_LAT_BAD = 1, 2, 3
DELAY_BINS_US = np.logspace(-1, 5, 121)  # 0.1 us .. 100 ms
KEY = ["skew", "queues", "policy", "rate_mpps"]


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def launch_app(args):
    command = f"{APP} {args}"
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
    if not ready.wait(timeout=90):
        stop_app(proc)
        raise RuntimeError("starvation app did not become ready:\n" + "".join(lines[-30:]))
    return proc


def stop_app(proc):
    proc.terminate()
    try:
        proc.wait(timeout=60)
    except sp.TimeoutExpired:
        sp.run(["sudo", "pkill", "-9", "-f", "starvation/build/starvation"])
        proc.wait()
    proc.reader.join(timeout=10)
    return "".join(proc.lines)


def parse_app_output(out):
    m = re.search(r"qprobe tsc_hz=(\d+) t0=(\d+) t_last=(\d+) records=(\d+) dropped=(\d+)", out)
    if not m:
        raise RuntimeError("no qprobe summary in the app output:\n" + out[-3000:])
    info = dict(zip(["tsc_hz", "t0", "t_last", "records", "dropped"], map(int, m.groups())))
    queues = [dict(q=int(a), pkts=int(b), polls=int(c), max_cyc=int(d), svc_cyc=int(e)) for a, b, c, d, e in
              re.findall(r"qdrain q=(\d+) pkts=(\d+) polls=(\d+) max_cyc=(\d+) svc_cyc=(\d+)", out)]
    hist = {int(b): int(n) for b, n in re.findall(r"qhist b=(\d+) pkts=(\d+)", out)}
    info["svc_hist"] = {int(b): int(n) for b, n in re.findall(r"qsvc b=(\d+) pkts=(\d+)", out)}
    thr = re.search(r"measured RX Throughput:\s*([\d.]+)", out)
    info["app_rx_pps"] = float(thr.group(1)) if thr else float("nan")
    info["samples"] = re.findall(r"qprobe sample len=(\d+) tail=([0-9a-f]+)", out)
    return info, queues, hist


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def read_log(path):
    with open(path, "rb") as f:
        hdr = np.frombuffer(f.read(32), dtype="<u8")
        rec = np.frombuffer(f.read(), dtype=REC_DTYPE)
    if hdr[0] != 0x003145424F525051:
        raise RuntimeError(f"{path}: not a qprobe log")
    return dict(tsc_hz=int(hdr[1]), t0=int(hdr[2])), rec


def lower_fit(x, y, windows=40):
    """Line under the cloud (x, y): fit through the minimum of y - line in
    equally populated windows of x, twice. Returns y - line (>= ~0)."""
    x = x.astype(np.float64)
    y = y.astype(np.float64)
    x0, y0 = x.min(), y.min()
    x, y = x - x0, y - y0
    if len(x) < 2 * windows or np.ptp(x) == 0:
        return y - y.min()
    a, b = (np.ptp(y) / np.ptp(x)), 0.0
    order = np.argsort(x)
    chunks = np.array_split(order, windows)
    for _ in range(3):
        r = y - (a * x + b)
        idx = np.array([c[np.argmin(r[c])] for c in chunks if len(c)])
        a, b = np.polyfit(x[idx], y[idx], 1)
    r = y - (a * x + b)
    return r - np.min([r[c].min() for c in chunks if len(c)])


def lower_fit_line(x, y, windows=40):
    """Like lower_fit but returns (a, b) of y ~ a x + b in the original units."""
    x = x.astype(np.float64)
    y = y.astype(np.float64)
    x0, y0 = x.min(), y.min()
    xs, ys = x - x0, y - y0
    a, b = (np.ptp(ys) / np.ptp(xs)), 0.0
    chunks = np.array_split(np.argsort(xs), windows)
    for _ in range(3):
        r = ys - (a * xs + b)
        idx = np.array([c[np.argmin(r[c])] for c in chunks if len(c)])
        a, b = np.polyfit(xs[idx], ys[idx], 1)
    r = ys - (a * xs + b)
    b += min(r[c].min() for c in chunks if len(c))
    return a, y0 + b - a * x0


def pct(values, ps=(50, 99, 99.9)):
    if len(values) == 0:
        return {f"p{p}": float("nan") for p in ps} | {"max": float("nan")}
    out = {f"p{p}": float(np.percentile(values, p)) for p in ps}
    out["max"] = float(np.max(values))
    return out


def seq_stats(seq_arrival, step=1):
    """Lost and reordered packets of one sequence, in arrival order. The TRex
    trailer seq starts at -2, so it is read as signed."""
    s = seq_arrival.astype(np.uint32).view(np.int32).astype(np.int64)
    uniq, first = np.unique(s, return_index=True)
    expected = (uniq[-1] - uniq[0]) // step + 1
    in_order = s[np.sort(first)]  # first copy of every seq: duplicates are not reordering
    return int(expected - len(uniq)), int(np.sum(np.diff(in_order) < 0)), int(len(s) - len(uniq))


def trex_cores(bg):
    """TRex splits a field-engine variable over its k DP cores by interleaving:
    core j sends j, j+k, j+2k... with its own timing, so seq % k is the
    substream. k is the smallest value that leaves (almost) no reordering
    inside the substreams of the sampled flows."""
    def reorders(k):
        total = 0
        for fid in np.unique(bg["flow_id"]):
            s = bg["seq"][bg["flow_id"] == fid].astype(np.int64)
            for c in np.unique(s % k):
                total += int(np.sum(np.diff(s[s % k == c]) < 0))
        return total
    counts = {k: reorders(k) for k in (1, 2, 4, 8, 16)}
    best = min(counts.values())
    return min(k for k, v in counts.items() if v <= best + max(10, best * 0.01))


def analyze(log_path, meta, info, base_oneway_us, k_trex=4):
    hdr, rec = read_log(log_path)
    hz = hdr["tsc_hz"]
    t0 = hdr["t0"]
    t_end = info.get("t_last") or int(rec["tsc"].max(initial=t0))
    rec = rec[(rec["tsc"] >= t0) & (rec["tsc"] <= t_end)]
    us = 1e6 / hz
    flows_meta = meta["flows"]
    flows, fct, delays = [], [], {"probe": [], "fct": [], "bg": []}

    # TRex TX timestamps -> host TSC, one fit for all latency streams
    latr = rec[rec["kind"] == KIND_LAT]
    if len(latr) > 100:
        a, b = lower_fit_line(latr["trex_ts"], latr["tsc"])
    else:
        a = b = None

    bg_all = rec[rec["kind"] == KIND_BG]
    if k_trex <= 0:  # auto: unreliable at low load, where the cores rarely interleave out of order
        k_trex = trex_cores(bg_all) if len(bg_all) else 1

    for fid in np.unique(rec["flow_id"]):
        fm = flows_meta.get(int(fid))
        if fm is None:
            continue
        r = rec[rec["flow_id"] == fid]
        queue = int(np.bincount(r["queue"]).argmax())
        row = dict(flow_id=int(fid), kind=fm["kind"], rank=fm.get("rank", ""),
                   size=fm.get("size", ""), queue=queue, records=len(r))

        if fm["kind"] in ("probe", "fct"):
            r = r[r["kind"] == KIND_LAT]
            if len(r) == 0 or a is None:
                continue
            tx = a * r["trex_ts"].astype(np.float64) + b
            d = (r["tsc"].astype(np.float64) - tx) * us
            d = np.maximum(d, 0.0)
            lost, reord, dup = seq_stats(r["seq"])
            row.update(lost=lost, reordered=reord, dup=dup, **pct(d))
            delays[fm["kind"]].append(d)

            if fm["kind"] == "fct":
                N = fm["size"]
                order = np.argsort(r["trex_ts"])
                txs, rxs, seqs = tx[order], r["tsc"].astype(np.float64)[order], r["seq"][order]
                gap_cyc = max(20e-6, 10.0 / fm["pps"]) * hz
                cut = np.nonzero(np.diff(txs) > gap_cyc)[0] + 1
                bursts = np.split(np.arange(len(txs)), cut)
                for k, idx in enumerate(bursts[1:-1], start=1):  # drop the edge bursts
                    n = len(idx)
                    ideal = (txs[idx[-1]] - txs[idx[0]]) * us
                    fct_us = (rxs[idx].max() - txs[idx[0]]) * us
                    fct.append(dict(flow_id=int(fid), size=N, queue=queue, burst=k, pkts=n,
                                    lost=max(0, N - n), ideal_us=ideal, fct_us=fct_us,
                                    slowdown=(fct_us + base_oneway_us) / (ideal + base_oneway_us)))
        else:  # sampled Zipf flow
            r = r[r["kind"] == KIND_BG]
            if len(r) < 4:
                continue
            step = int(np.lcm(k_trex, 1 << fm["shift"]))
            seg = r["seq"].astype(np.int64) % k_trex
            lost = reord = dup = 0
            ds = []
            for s in np.unique(seg):
                rs = r[seg == s]
                if len(rs) < 4:
                    continue
                l, o, u = seq_stats(rs["seq"], step)
                lost, reord, dup = lost + l, reord + o, dup + u
                ds.append(lower_fit(rs["seq"], rs["tsc"]) * us)
            d = np.concatenate(ds) if ds else np.array([])
            row.update(lost=lost, reordered=reord, dup=dup, segments=int(len(np.unique(seg))),
                       samples_expected=lost + len(r) - dup, **pct(d))
            if len(d):
                delays["bg"].append(d)
        flows.append(row)

    hists = {k: np.histogram(np.concatenate(v) if v else np.array([]), bins=DELAY_BINS_US)[0]
             for k, v in delays.items()}
    extra = dict(records=len(rec), lat_bad=int(np.sum(rec["kind"] == KIND_LAT_BAD)), trex_cores=k_trex,
                 clock_ratio=a if a is not None else float("nan"))
    return flows, fct, hists, extra


# ---------------------------------------------------------------------------
# TRex
# ---------------------------------------------------------------------------

def load_streams(client, skew, args):
    streams, meta = profile.build(skew=skew, num_flows=args.flows, ref_mpps=max(args.rates))
    client.stop(ports=PORTS)
    client.reset(ports=PORTS)
    client.add_streams(streams, ports=PORTS)
    return meta


def pg_stats(client, meta):
    pg_ids = sorted(f["pg_id"] for f in meta["flows"].values() if "pg_id" in f)
    pg = client.get_pgid_stats(pg_ids)
    out = {}
    for pid in pg_ids:
        latency = lat.pg_entry(pg.get("latency", {}), pid).get("latency", {})
        flow = lat.pg_entry(pg.get("flow_stats", {}), pid)
        p = lat.histogram_percentiles(latency.get("histogram", {})) or {}
        out[pid] = dict(trex_tx=lat.counter_total(flow.get("tx_pkts", {})),
                        trex_rx=lat.counter_total(flow.get("rx_pkts", {})),
                        rtt_min=float(latency.get("total_min", "nan")),
                        rtt_avg=float(latency.get("average", "nan")),
                        rtt_p99=p.get(99, float("nan")), rtt_max=p.get("max", float("nan")))
    return out


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


def done_points(directory):
    path = os.path.join(directory, "runs.csv")
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return {(float(r["skew"]), int(r["queues"]), r["policy"], float(r["rate_mpps"]))
                for r in csv.DictReader(f)}


def run_point(client, meta, args, skew, queues, policy, rate, writer, logdir):
    key = dict(skew=skew, queues=queues, policy=policy, rate_mpps=rate)
    log_path = os.path.join(logdir, f"s{skew}_q{queues}_{policy}_r{rate}.bin")
    proc = launch_app(f"-q {queues} --policy {policy} --log {log_path}")
    try:
        client.clear_stats(ports=PORTS)
        client.start(ports=PORTS, mult=str(rate * 1e6 / args.flows), force=True)
        sleep(args.warmup)
        client.clear_stats(ports=PORTS)
        client.clear_pgid_stats(clear_flow_stats=True, clear_latency_stats=True)
        sleep(args.duration)
        port = client.get_stats(ports=PORTS)[PORTS[0]]
        pgs = pg_stats(client, meta)
        client.stop(ports=PORTS)
        sleep(0.5)
    finally:
        client.stop(ports=PORTS)
        out = stop_app(proc)

    info, queues_rows, hist = parse_app_output(out)
    tx, rx = float(port["opackets"]), float(port["ipackets"])
    loss = max(0.0, (tx - rx) / tx * 100) if tx else float("nan")

    rtt_min = [v["rtt_min"] for v in pgs.values() if v["rtt_min"] == v["rtt_min"] and v["rtt_min"] > 0]
    base_oneway = min(rtt_min) / 2 if rtt_min else 5.0
    flows, fct, hists, extra = analyze(log_path, meta, info, base_oneway, args.trex_cores)

    hz = info["tsc_hz"]
    dur = max(1e-9, (info["t_last"] - info["t0"]) / hz)
    qrows = []
    for q in queues_rows:
        load = q["pkts"] / dur
        max_us = q["max_cyc"] / hz * 1e6
        qrows.append(key | dict(q=q["q"], pkts=q["pkts"], polls=q["polls"], load_pps=load,
                                max_gap_us=max_us, max_svc_us=q["svc_cyc"] / hz * 1e6,
                                overflow_us=RX_DESC / load * 1e6 if load else ""))
    busy = [r for r in qrows if r["polls"] >= 10]

    def hist_pct(h, p, cap):
        """Percentile of a log2(cycles) histogram, at the geometric centre of
        the bucket and never above the measured maximum."""
        total, c = sum(h.values()), 0
        for b in sorted(h):
            c += h[b]
            if c >= p * total:
                return min(cap, 2 ** (b + 0.5) / hz * 1e6)
        return float("nan")

    for f in flows:
        if "pg_id" in meta["flows"].get(f["flow_id"], {}):
            f.update(pgs[meta["flows"][f["flow_id"]]["pg_id"]])
    probe_d = hists["probe"]
    run = key | dict(
        tx_pps=tx / args.duration, rx_pps=rx / args.duration, loss_pct=loss,
        app_rx_pps=info["app_rx_pps"], records=info["records"], log_dropped=info["dropped"],
        lat_bad=extra["lat_bad"], trex_cores=extra["trex_cores"], clock_ratio=extra["clock_ratio"],
        base_oneway_us=base_oneway,
        drain_max_us=max((r["max_gap_us"] for r in busy), default=float("nan")),
        drain_p99_us=hist_pct(hist, 0.99, max((r["max_gap_us"] for r in busy), default=float("nan"))),
        drain_p999_us=hist_pct(hist, 0.999, max((r["max_gap_us"] for r in busy), default=float("nan"))),
        svc_max_us=max((r["max_svc_us"] for r in busy), default=float("nan")),
        svc_p99_us=hist_pct(info["svc_hist"], 0.99, max((r["max_svc_us"] for r in busy), default=float("nan"))),
        svc_p999_us=hist_pct(info["svc_hist"], 0.999, max((r["max_svc_us"] for r in busy), default=float("nan"))),
        overflow_queues=sum(1 for r in busy if r["overflow_us"] != "" and r["max_gap_us"] > r["overflow_us"]),
        probe_lost=sum(f.get("lost", 0) for f in flows if f["kind"] == "probe"),
        bg_lost=sum(f.get("lost", 0) for f in flows if f["kind"] == "bg"),
        bg_samples=sum(f.get("samples_expected", 0) for f in flows if f["kind"] == "bg"),
        reordered=sum(f.get("reordered", 0) for f in flows),
        probe_pkts=int(probe_d.sum()),
    )
    writer.write("runs.csv", [run])
    writer.write("queues.csv", qrows)
    writer.write("flows.csv", [key | f for f in flows])
    writer.write("fct.csv", [key | f for f in fct])
    writer.write("delayhist.csv", [key | dict(kind=k, lo_us=DELAY_BINS_US[i], hi_us=DELAY_BINS_US[i + 1], n=int(n))
                                   for k, h in hists.items() for i, n in enumerate(h) if n])
    writer.write("drainhist.csv", [key | dict(gap=g, bucket=b, lo_us=2 ** b / hz * 1e6, pkts=n)
                                   for g, h in (("drain", hist), ("service", info["svc_hist"]))
                                   for b, n in h.items()])
    if info["samples"]:
        tqdm.write("  trailer samples: " + "; ".join(f"len={l} {t}" for l, t in info["samples"][:2]))
    if not args.keep_logs:
        os.remove(log_path)
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queues", type=int, nargs="+", default=[1, 64, 128, 1024, 2048])
    parser.add_argument("--policies", nargs="+", default=POLICIES, choices=POLICIES)
    parser.add_argument("--skews", type=float, nargs="+", default=[0.1, 0.6, 0.9])
    parser.add_argument("--rates", type=float, nargs="+", default=[1.0, 2.0, 3.0],
                        help="offered Zipf rate in Mpps (probes and finite flows come on top)")
    parser.add_argument("--flows", type=int, default=10000)
    parser.add_argument("--warmup", type=float, default=3.5, help="seconds (the app warms up for 3)")
    parser.add_argument("--duration", type=float, default=8.0, help="seconds per point")
    parser.add_argument("--keep-logs", action="store_true", help="keep the binary qprobe logs")
    parser.add_argument("--trex-cores", type=int, default=4,
                        help="TRex DP cores per port, which split the field-engine seq (0: detect)")
    parser.add_argument("--output", default="results/starvation", help="relative to experiments/")
    parser.add_argument("--analyze", metavar="LOG", help="only analyse a kept log (needs --skews)")
    args = parser.parse_args()

    if args.analyze:
        _, meta = profile.build(skew=args.skews[0], num_flows=args.flows, ref_mpps=max(args.rates))
        flows, fct, hists, extra = analyze(args.analyze, meta, {}, 5.0, args.trex_cores)
        for f in flows:
            print(f)
        print(f"{len(fct)} bursts, extra {extra}")
        return

    out_dir = os.path.join(HERE, args.output)
    logdir = os.path.join(out_dir, "logs")
    os.makedirs(logdir, exist_ok=True)
    writer = Writer(out_dir)
    done = done_points(out_dir)

    points = [(s, q, p, r) for s in args.skews for q in args.queues for p in args.policies for r in args.rates]
    todo = [pt for pt in points if pt not in done]
    tqdm.write(f"{len(todo)} points to run ({len(points) - len(todo)} already in {out_dir})")

    client = STLClient(server=TREX_SERVER)
    client.connect()
    client.acquire(ports=PORTS, force=True)
    ndz.set_governor("performance")
    try:
        pbar = tqdm(total=len(todo), unit="pt")
        loaded = None
        for skew, queues, policy, rate in todo:
            if loaded != skew:
                meta = load_streams(client, skew, args)
                loaded = skew
            pbar.set_description(f"s={skew} Q={queues} {policy} {rate}Mpps")
            for attempt in range(2):
                try:
                    run = run_point(client, meta, args, skew, queues, policy, rate, writer, logdir)
                    break
                except Exception as e:  # noqa: BLE001
                    tqdm.write(f"⚠️ point failed ({e}); {'retrying' if attempt == 0 else 'skipped'}")
                    sp.run(["sudo", "pkill", "-9", "-f", "starvation/build/starvation"])
                    sleep(3)
            else:
                pbar.update(1)
                continue
            tqdm.write(f"  loss={run['loss_pct']:.3f}% drain max/p99.9={run['drain_max_us']:.0f}/"
                       f"{run['drain_p999_us']:.0f}us service max/p99.9={run['svc_max_us']:.0f}/"
                       f"{run['svc_p999_us']:.0f}us overflow_q={run['overflow_queues']} "
                       f"probe_lost={run['probe_lost']} bg_lost={run['bg_lost']}/{run['bg_samples']} "
                       f"reord={run['reordered']} lat_bad={run['lat_bad']} log_drop={run['log_dropped']}")
            pbar.update(1)
        pbar.close()
    finally:
        ndz.release_trex(client)
        ndz.set_governor("schedutil")
    tqdm.write(f"Results in {out_dir}/")


if __name__ == "__main__":
    t = time.time()
    main()
    tqdm.write(f"elapsed {time.time() - t:.0f}s")
