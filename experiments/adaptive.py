"""
Reaction of the adaptive app (adaptive/build/adaptive) to changing traffic.

A scenario is a sequence of phases; each phase sets the offered rate, the Zipf
skew, the number of flows and the packet size of the Zipf load of
zipf-profile.py. The app runs once for the whole scenario while TRex switches
phase: a rate change is applied on the fly, a skew, flow or size change reloads
the streams (the app then sees a short gap without traffic). Every scenario is
run with --adaptive and, for comparison, with a fixed number of queues.

Every second the script samples TRex (offered and returned pps, loss) and the
app's per-second statistics (forwarded pps, empty polls, pkts/burst, active
and polled queues), and logs every queue change the controller makes.

Results in <output>/:
  runs.csv       one row per run
  logs/          the app's adaptive and RSS lines, with seconds since traffic start
  timeline.csv   one row per second: phase, TRex rates, app statistics, queues
  decisions.csv  every queue change of the controller, with its cost
  snapshots.csv  with --snapshot-ms: packets waiting in every active queue at a
                 sweep start, the next 48 polls that returned packets
                 (queue:packets) and the empty polls skipped among them
  phases.csv     per run and phase: means over the whole phase and over its
                 second half (steady state), queue count the controller
                 settles on and time to reach it
Plots: plot_adaptive.py.

Examples:
  python3 adaptive.py --scenarios load --static 128 --phase-duration 10
  python3 adaptive.py --variants "" "--adaptive-epoch 50" "--adaptive-threshold 1"
  python3 adaptive.py                         # every scenario, default comparison
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
from collections import Counter
from time import sleep

from tqdm import tqdm

sys.path.append("/trex/v3.08/automation/trex_control_plane/interactive")
from trex.stl.api import STLClient, STLProfile  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location("no_drop_zip", os.path.join(HERE, "no-drop-zip.py"))
ndz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ndz)

TREX_SERVER = "100.78.72.16"
PORTS = [0]
PROFILE_FILE = os.path.join(HERE, "zipf-profile.py")
APP = (ndz.NF_CHAIN.format(lcores="0")
       .replace("(0,0,0),(0,1,1)", "(0,0,0)")
       .replace("/nf-chain/build/nf-chain", "/adaptive/build/adaptive"))
APP_PATTERN = "adaptive/build/adaptive"
POLICIES = ["baseline", "aggressive", "penalty", "aggressive+penalty"]
MAX_FLOWS = 20000  # zipf-profile.py caps the flows here

# Offered rate above the single-core capacity of nf-chain (~3-5 Mpps): the
# returned rate is then the app's throughput.
SAT = 8.0


def phase(rate, skew=0.6, flows=10000, size=64):
    """rate in Mpps (0 stops the traffic)."""
    return dict(rate=rate, skew=skew, flows=flows, size=size)


SCENARIOS = {
    # flows changing at a rate every policy sustains with the right queue count
    # (zero-loss: aggressive 3.2-4.3 Mpps, baseline and penalty 2.6-4.2 Mpps
    # from 15000 to 100 flows); at 3 Mpps 15000 flows pushed the QDMA queues
    # into the overload stall
    "flows-rate": [phase(2.5, flows=f) for f in (100, 1000, 5000, 10000, 15000, 5000, 1000, 100)],
    # the same flows at 3 Mpps, where 1 queue is overloaded from 5000 flows on
    "flows-rate3": [phase(3.0, flows=f) for f in (100, 1000, 5000, 10000, 15000, 5000, 1000, 100)],
    # load staircase up and down: idle-ish, below and above capacity
    "load": [phase(r) for r in (0.5, 1, 2, 3, 4, SAT, 4, 2, 1, 0.5)],
    # finer steps around the single-core capacity (~2.4-3.4 Mpps), up and down
    "ramp": [phase(r) for r in (0.5, 1, 1.5, 2, 2.5, 3, 3.25, 3.5, 3.25, 3, 2.5, 2, 1, 0.5)],
    # same overload, more and less skewed traffic
    "skew": [phase(SAT, skew=s) for s in (0.1, 0.6, 0.9, 0.6, 0.1)],
    # same overload, more and fewer active flows (the NAT table holds 16384:
    # above that every packet takes the slow path and nf-chain drops to ~1 Mpps)
    "flows": [phase(SAT, flows=f) for f in (100, 1000, 10000, 15000, 1000)],
    # traffic that stops and restarts, with a light phase in between
    "onoff": [phase(r) for r in (SAT, 0, SAT, 0, 0.5, SAT)],
    # a skewed load at moderate rate
    "skew-load": [phase(r, skew=s) for s, r in ((0.9, 1), (0.9, 3), (0.1, 3), (0.1, 1), (0.6, SAT))],
    # larger packets: fewer pps for the same bandwidth, more bytes per packet
    "size": [phase(SAT, size=s) for s in (64, 256, 1024, 64)],
    # everything changes at once
    "mixed": [phase(1, 0.9, 1000), phase(SAT, 0.1, 15000), phase(3, 0.6, 10000),
              phase(SAT, 0.9, 100), phase(0.5, 0.1, 10000), phase(SAT, 0.6, 10000)],
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
            cur = dict(t=t - t0, app_rx_pps=to_int(m.group(2)))
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
    """Loads the Zipf streams of a phase and sets its rate; streams are built
    once per (skew, flows, size) and kept."""

    def __init__(self, client, start_spread_us=0):
        self.client = client
        self.start_spread_us = start_spread_us
        self.cache = {}
        self.loaded = None
        self.running = False

    def streams(self, ph):
        key = (ph["skew"], min(ph["flows"], MAX_FLOWS), ph["size"])
        if key not in self.cache:
            self.cache[key] = STLProfile.load_py(PROFILE_FILE, direction=0, port_id=0, skew=key[0],
                                                 num_flows=key[1], packet_size=key[2],
                                                 start_spread_us=self.start_spread_us).get_streams()
        return key, self.cache[key]

    def stop(self):
        self.client.stop(ports=PORTS)
        self.running = False

    def apply(self, ph):
        """Switch to phase ph; returns the seconds spent reloading streams."""
        if ph["rate"] <= 0:
            self.stop()
            return 0.0
        mult = f"{int(ph['rate'] * 1e6)}pps"
        key, streams = self.streams(ph)
        if key == self.loaded and self.running:
            self.client.update(ports=PORTS, mult=mult, force=True)
            return 0.0
        t = time.monotonic()
        if key != self.loaded:
            self.stop()
            self.client.reset(ports=PORTS)
            self.client.add_streams(streams, ports=PORTS)
            self.loaded = key
        self.client.start(ports=PORTS, mult=mult, force=True)
        self.running = True
        return time.monotonic() - t


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

def run_scenario(client, traffic, args, scenario, policy, mode, variant, rep, writer):
    key = dict(scenario=scenario, policy=policy, mode=mode, variant=variant, rep=rep)
    if mode == "adaptive":
        app_args = f"-q {args.max_queues} --policy {policy} --adaptive {variant}"
    else:
        app_args = f"-q {mode.split('-')[1]} --policy {policy}"
    if args.snapshot_ms:
        app_args += f" --snapshot-ms {args.snapshot_ms}"
    phases = SCENARIOS[scenario]

    # build every stream set before the app starts
    for ph in phases:
        if ph["rate"] > 0:
            traffic.streams(ph)
    traffic.stop()

    proc = launch_app(app_args)
    samples, bounds = [], []
    try:
        sleep(args.idle)  # the app starts with no traffic
        t0 = time.monotonic()
        last = None
        for i, ph in enumerate(phases):
            reload_s = traffic.apply(ph)
            start = time.monotonic() - t0
            bounds.append(dict(phase=i, start=start, reload_s=reload_s, **ph))
            end = start + args.phase_duration
            while True:
                now = time.monotonic() - t0
                if now >= end:
                    break
                sleep(min(1.0, end - now))
                st = client.get_stats(ports=PORTS)[PORTS[0]]
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
                last = row
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
            settled_queues=settled, time_to_settle_s=round(reach, 2) if reach != "" else "",
            changes=len(in_phase), queues_seen=" ".join(str(q) for q in sorted(set(qs)))))

    run = key | dict(
        app_args=app_args, phases=len(phases), phase_duration=args.phase_duration,
        rx_pps=sum(r["rx_pps"] for r in timeline) / max(1, len(timeline)),
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
            if name.startswith("adaptive"):
                s += f" (q={r['settled_queues']} in {r['time_to_settle_s']}s, {r['changes']} changes)"
            parts.append(s)
        tqdm.write(f"  {desc}: " + ", ".join(parts) + " Mpps")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    parser.add_argument("--policies", nargs="+", default=["aggressive+penalty"], choices=POLICIES)
    parser.add_argument("--static", type=int, nargs="*", default=[1, 128, 2048],
                        help="fixed queue counts run for comparison (none: only adaptive)")
    parser.add_argument("--variants", nargs="+", default=[""],
                        help="extra adaptive options, one run each, e.g. \"--adaptive-epoch 50\"")
    parser.add_argument("--max-queues", type=int, default=2048, help="-q of the adaptive runs")
    parser.add_argument("--phase-duration", type=float, default=20.0, help="seconds per phase")
    parser.add_argument("--idle", type=float, default=2.0, help="seconds without traffic after startup")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--snapshot-ms", type=int, default=0,
                        help="queue snapshots of the app every this many ms (snapshots.csv), 0 = none")
    parser.add_argument("--start-spread-us", type=float, default=0.0,
                        help="spread the start of the flows over this many us (zipf-profile.py), so that "
                             "a stream reload does not send the first packet of every flow at once")
    parser.add_argument("--output", default="results/adaptive/runs", help="relative to experiments/")
    args = parser.parse_args()

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
    traffic = Traffic(client, args.start_spread_us)
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
                               f"{run['changes']} queue changes")
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
