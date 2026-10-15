"""
Zero-loss throughput of the adaptive app against the fixed queue counts, while
the number of active flows changes.

For every number of flows, policy and mode the app (adaptive/build/adaptive)
runs once and a binary search finds the highest Zipf rate it forwards without
loss. The modes are:

  signal       --adaptive (backlog share and empty polls)
  static-N     N fixed queues, no controller (the queue sweep)

A search trial starts TRex at the trial rate, waits --warmup seconds (the
controller settles; the app keeps the queue count it had at the previous
trial), then counts the packets sent and received over the next --duration
seconds without stopping the traffic (counter differences; the packets in
flight at the two reads are a few hundred, ~0.001%). The flows start spread
over --start-spread-us (zipf-profile.py), and the start falls in the warmup
anyway: when TRex starts all the flows at once, their first packets form a
burst of one packet per flow that one RX ring cannot hold (~0.7 packets lost
per flow with 1 queue), which an earlier version counted in every trial. A
trial passes when the loss is at most --max-loss percent.
After the search the app runs once more at the zero-loss rate, and the script
records the queue count the controller uses there, its changes and the poll
mix (empty and full polls, busy fraction of the core).

Results in <output>/:
  ndr.csv     one row per (flows, policy, mode, rep): zero-loss rate, trials,
              queues used at that rate, poll mix
  trials.csv  every trial: rate, loss, queues at the end of the trial
Plots and the comparison with the queue sweep: plot_adaptive_ndr.py.

Examples:
  python3 adaptive_ndr.py --flows 10000 --policies aggressive --modes signal static-64
  python3 adaptive_ndr.py                    # the full campaign
"""

import argparse
import csv
import importlib.util
import os
import re
import subprocess as sp
import sys
import time
from collections import Counter
from time import sleep

from tqdm import tqdm

sys.path.append("/trex/v3.08/automation/trex_control_plane/interactive")
from trex.stl.api import STLClient, STLProfile  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location("adaptive_campaign", os.path.join(HERE, "adaptive.py"))
adp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adp)
ndz = adp.ndz

PORTS = adp.PORTS
POLICIES = ["baseline", "aggressive", "penalty"]
STATIC = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048]
MODES = ["signal"] + [f"static-{q}" for q in STATIC]

RE_RSS = re.compile(r"RSS queues: (\d+) \(polled (\d+)\)(?:, adaptive (\w+), (\d+) changes)?")
RE_MIX = re.compile(r"poll mix: active polls (\d+), empty ([\d.]+)%, full ([\d.]+)%, busy ([\d.]+)"
                    r"(?:, backlog max ([\d.]+)%, over half ([\d.]+)%)?")


def app_args(mode, policy, max_queues, signal_args=""):
    if mode == "signal":
        return f"-q {max_queues} --policy {policy} --adaptive {signal_args}".strip()
    return f"-q {int(mode.split('-')[1])} --policy {policy}"


def app_window(proc, mark):
    """Queue counts, controller changes and poll mix printed since line @mark."""
    queues, changes, mix = [], [], []
    for _, line in proc.lines[mark:]:
        if m := RE_RSS.search(line):
            queues.append(int(m.group(1)))
            if m.group(4) is not None:
                changes.append(int(m.group(4)))
        elif m := RE_MIX.search(line):
            mix.append([float(m.group(i) or 0) for i in (2, 3, 4, 5, 6)])
    mix = mix[1:-1] or mix  # the first and last seconds straddle the window
    mean = lambda i: sum(r[i] for r in mix) / len(mix) if mix else ""  # noqa: E731
    return dict(queues=Counter(queues).most_common(1)[0][0] if queues else "",
                queues_seen=" ".join(str(q) for q in sorted(set(queues))),
                changes=changes[-1] - changes[0] if len(changes) > 1 else 0,
                empty_pct=mean(0), full_pct=mean(1), busy=mean(2),
                backlog_max_pct=max((r[3] for r in mix), default=""), backlog_half_pct=mean(4))


def run_steady(client, pps, warmup, seconds, proc):
    """Packets (tx, rx) over @seconds of traffic at @pps, after @warmup seconds
    of the same traffic, and the index of the first app line of the measured
    part; the traffic is stopped at the end."""
    client.clear_stats(ports=PORTS)
    client.start(ports=PORTS, mult=f"{int(pps)}pps", force=True)
    sleep(warmup)
    mark = len(proc.lines)
    st = client.get_stats(ports=PORTS)[PORTS[0]]
    tx0, rx0 = st["opackets"], st["ipackets"]
    sleep(seconds)
    st = client.get_stats(ports=PORTS)[PORTS[0]]
    client.stop(ports=PORTS)
    return st["opackets"] - tx0, st["ipackets"] - rx0, mark


def trial(client, proc, pps, args):
    tx, rx, mark = run_steady(client, pps, args.warmup, args.duration, proc)
    if tx > 0 and rx == 0:
        # the QDMA queues sometimes stop delivering after an overload, until
        # the app restarts: retry the point instead of recording a zero rate
        raise RuntimeError(f"nothing forwarded at {pps / 1e6:.3f} Mpps: app or NIC stalled")
    loss = max(0.0, (tx - rx) / tx * 100) if tx else 100.0
    return dict(rate_mpps=pps / 1e6, tx=tx, rx=rx, loss_pct=loss,
                passed=tx > 0 and loss <= args.max_loss, **app_window(proc, mark))


def search(client, proc, args, log):
    """Binary search of the highest passing rate in [lo, hi] Mpps."""
    lo, hi = args.lo * 1e6, args.hi * 1e6
    trials = []

    def run(pps):
        t = trial(client, proc, pps, args)
        trials.append(t)
        log(t)
        return t["passed"]

    if run(hi):
        return hi, trials
    if not run(lo):
        # below the lower bound: halve until a rate passes
        while lo > 0.05e6:
            hi, lo = lo, lo / 2
            if run(lo):
                break
        else:
            return 0.0, trials
    while (hi - lo) > args.precision * 1e6 and len(trials) < args.max_trials:
        mid = (lo + hi) / 2
        if run(mid):
            lo = mid
        else:
            hi = mid
    return lo, trials


def done_points(path):
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return {(int(r["flows"]), r["policy"], r["mode"], int(r["rep"])) for r in csv.DictReader(f)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flows", type=int, nargs="+", default=[100, 1000, 5000, 10000, 15000],
                        help="active flows (the NAT table holds 16384)")
    parser.add_argument("--skew", type=float, default=0.6)
    parser.add_argument("--policies", nargs="+", default=POLICIES, choices=adp.POLICIES)
    parser.add_argument("--modes", nargs="+", default=MODES,
                        help="signal and static-N (default: signal and every power of 2 to 2048)")
    parser.add_argument("--max-queues", type=int, default=2048, help="-q of the adaptive modes")
    parser.add_argument("--signal-args", default="", help="extra app options of the signal mode (thresholds)")
    parser.add_argument("--lo", type=float, default=1.0, help="Mpps, lower bound of the search")
    parser.add_argument("--hi", type=float, default=4.5, help="Mpps, upper bound of the search")
    parser.add_argument("--precision", type=float, default=0.02, help="Mpps")
    parser.add_argument("--max-trials", type=int, default=12)
    # 0.1%: the pdr of the NdrBench search in no-drop-zip.py, so that the queue
    # sweep compares with the classic NDR campaign. With 32 or more queues the
    # NIC drops 25-60 packets per second whatever the rate (0.002-0.004%), well
    # below it
    parser.add_argument("--max-loss", type=float, default=0.1, help="percent of the packets of a trial")
    parser.add_argument("--warmup", type=float, default=5.0, help="seconds at the trial rate before measuring")
    parser.add_argument("--duration", type=float, default=10.0, help="seconds measured per trial")
    parser.add_argument("--final", type=float, default=10.0, help="seconds at the zero-loss rate at the end")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", default="results/adaptive/ndr_v2", help="relative to experiments/")
    parser.add_argument("--start-spread-us", type=float, default=20000.0,
                        help="spread the start of the flows over this many us (zipf-profile.py)")
    args = parser.parse_args()
    for m in args.modes:
        if m != "signal" and not re.fullmatch(r"static-\d+", m):
            parser.error(f"unknown mode {m}")

    out_dir = os.path.join(HERE, args.output)
    os.makedirs(os.path.join(out_dir, "logs"), exist_ok=True)
    writer = adp.Writer(out_dir)
    done = done_points(os.path.join(out_dir, "ndr.csv"))
    points = [(f, p, m, r) for f in args.flows for p in args.policies for m in args.modes
              for r in range(args.repetitions)]
    todo = [x for x in points if x not in done]
    tqdm.write(f"{len(todo)} points to run ({len(points) - len(todo)} already in {out_dir})")

    client = STLClient(server=adp.TREX_SERVER)
    client.connect()
    client.acquire(ports=PORTS, force=True)
    client.reset(ports=PORTS)
    ndz.set_governor("performance")
    old_boost = ndz.set_boost(0)  # as the queue sweep of no-drop-zip.py
    loaded = None
    try:
        pbar = tqdm(total=len(todo), unit="pt")
        for flows, policy, mode, rep in todo:
            key = dict(flows=flows, skew=args.skew, policy=policy, mode=mode, rep=rep)
            pbar.set_description(f"{flows} flows {policy} {mode}")
            if loaded != flows:
                client.stop(ports=PORTS)
                client.reset(ports=PORTS)
                streams = STLProfile.load_py(adp.PROFILE_FILE, direction=0, port_id=0, skew=args.skew,
                                             num_flows=flows, packet_size=64,
                                             start_spread_us=args.start_spread_us).get_streams()
                client.add_streams(streams, ports=PORTS)
                loaded = flows
            for attempt in range(3):
                proc = None
                try:
                    proc = adp.launch_app(app_args(mode, policy, args.max_queues, args.signal_args))
                    sleep(2)
                    t0 = time.monotonic()

                    def log(t):
                        tqdm.write(f"  {t['rate_mpps']:.3f} Mpps: loss {t['loss_pct']:.4f}% "
                                   f"q={t['queues']} ({t['queues_seen']}) empty {t['empty_pct']}% "
                                   f"full {t['full_pct']}% busy {t['busy']} backlog {t['backlog_max_pct']}% -> {'ok' if t['passed'] else 'LOSS'}")
                        writer.write("trials.csv", [key | dict(t=round(time.monotonic() - t0, 1)) | t])

                    ndr, trials = search(client, proc, args, log)
                    final = {}
                    if ndr > 0:
                        tx, rx, mark = run_steady(client, ndr, args.warmup, args.final, proc)
                        final = app_window(proc, mark)
                        final["final_loss_pct"] = max(0.0, (tx - rx) / tx * 100) if tx else ""
                    row = key | dict(ndr_mpps=ndr / 1e6, trials=len(trials),
                                     elapsed_s=round(time.monotonic() - t0, 1), app_args=app_args(mode, policy, args.max_queues, args.signal_args),
                                     **{k: final.get(k, "") for k in ("queues", "queues_seen", "changes", "empty_pct",
                                                                      "full_pct", "busy", "backlog_max_pct",
                                                                      "backlog_half_pct", "final_loss_pct")})
                    writer.write("ndr.csv", [row])
                    tqdm.write(f"=> {flows} flows {policy} {mode}: NDR {ndr / 1e6:.3f} Mpps, "
                               f"queues {row['queues']} ({row['queues_seen']})")
                    break
                except Exception as e:  # noqa: BLE001
                    tqdm.write(f"⚠️ point failed ({e!r}); {'retrying' if attempt < 2 else 'skipped'}")
                    sp.run(["sudo", "pkill", "-9", "-f", adp.APP_PATTERN])
                    sleep(3)
                finally:
                    client.stop(ports=PORTS)
                    if proc is not None:
                        lines = adp.stop_app(proc)
                        name = f"f{flows}_{policy}_{mode}_r{rep}.log"
                        with open(os.path.join(out_dir, "logs", name), "w") as f:
                            f.writelines(l for _, l in lines if "adaptive:" in l or "RSS" in l or "poll mix" in l)
            pbar.update(1)
        pbar.close()
    finally:
        ndz.set_governor("schedutil")
        if old_boost is not None:
            ndz.set_boost(old_boost)
        ndz.release_trex(client)
    tqdm.write(f"Results in {out_dir}/")


if __name__ == "__main__":
    t = time.time()
    main()
    tqdm.write(f"elapsed {time.time() - t:.0f}s")
