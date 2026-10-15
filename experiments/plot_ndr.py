"""
Plots for the zero-loss campaign of no-drop-zip.py: throughput and the
per-packet perf events measured at the NDR rate, against the number of queues.

  python3 plot_ndr.py                  # every results/ndr/results_<experiment>_skew* found
  python3 plot_ndr.py --apps nf-chain  # only the NF chain

Writes PDF and PNG into plots/ndr/:
  <app>_<metric>             single core, one panel per trace, one line per policy
                             (paper Fig. 3, 5 and 8)
  <app>_overview             the same, every metric in one grid
  <app>-<multicore>_<metric> Shared/Partitioned, one line per core count; the
                             1-core line is the single-core aggressive+penalty run
                             (paper Fig. 7 and 9)
  <app>-<multicore>_overview the same, every metric in one grid
  <app>_shared_vs_partitioned
                             Shared vs Partitioned at 64 queues, 2 cores (Fig. 10)

Repetitions whose NDR search failed (final drop > 0.2%) are left out.
Lines are the mean over repetitions, the band is ±1 standard deviation.
Per-packet events are normalized by the packets TRex sent during the perf
window; in multicore they are summed over the polling cores.
"""

import argparse
import csv
import glob
import os
import re
import statistics
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# Same palette slots, markers and dashes as plot_latency.py
STYLES = [
    dict(color="#2a78d6", marker="o", dash=(None, None)),
    dict(color="#eb6834", marker="s", dash=(4, 2)),
    dict(color="#1baf7a", marker="^", dash=(1, 2)),
    dict(color="#eda100", marker="D", dash=(6, 2, 1, 2)),
]
POLICIES = ["baseline", "aggressive", "penalty", "aggressive+penalty"]
CORES = [1, 2, 4, 8]
SKEWS = {"0.1": "Uniform", "0.6": "Low skew (α = 0.6)", "0.9": "High skew (α = 0.9)"}
NDR_FAILED_DROP_PCT = 0.2
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"

plt.rcParams.update({
    "font.size": 9,
    "axes.edgecolor": INK_2,
    "axes.labelcolor": INK,
    "axes.titlesize": 9,
    "axes.titlecolor": INK,
    "xtick.color": INK_2,
    "ytick.color": INK_2,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "legend.frameon": False,
    "pdf.fonttype": 42,
})


def _f(row, key):
    v = row.get(key, "")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ratio(a, b):
    return a / b if a is not None and b else None


def _sum(*vals):
    return None if any(v is None for v in vals) else sum(vals)


# name -> (label, function of a raw CSV row, single core only)
METRICS = {
    "throughput": ("Throughput (Mpps)", lambda r: _ratio(_f(r, "rx_pps"), 1e6), False),
    "cycles": ("Cycles / packet", lambda r: _f(r, "cycles"), False),
    "instructions": ("Instructions / packet", lambda r: _f(r, "instructions"), False),
    "ipc": ("Instructions / cycle", lambda r: _ratio(_f(r, "instructions"), _f(r, "cycles")), False),
    "l1_misses": ("L1d load misses / packet", lambda r: _f(r, "L1_dcache_load_misses"), False),
    "l2_misses": ("L2 misses / packet", lambda r: _f(r, "l2_request_miss"), False),
    "llc_misses": ("LLC load misses / packet", lambda r: _f(r, "LLC_load_misses"), False),
    "dram_loads": ("Loads served by DRAM / packet",
                   lambda r: _f(r, "mem_load_l3_miss_retired_local_dram"), False),
    "dram_bytes": ("DRAM traffic (B / packet)",
                   lambda r: _sum(_f(r, "dram_read_bytes"), _f(r, "dram_write_bytes")), False),
    "dtlb_misses": ("dTLB load misses / packet", lambda r: _f(r, "dTLB_load_misses"), False),
    "branch_misses": ("Branch misses / packet", lambda r: _f(r, "branch_misses"), False),
    "empty_polls": ("Empty polls / s (millions)", lambda r: _ratio(_f(r, "empty_poll"), 1e6), True),
    "pkts_per_burst": ("Packets / burst", lambda r: _f(r, "packet_per_burst"), True),
}


def load(results_root):
    """{experiment: {skew: {(policy, queue): [row, ...]}}}"""
    data = defaultdict(dict)
    for path in glob.glob(os.path.join(results_root, "results_*_skew*", "benchmark_results.csv")):
        m = re.match(r"results_(.+)_skew([\d.]+)$", os.path.basename(os.path.dirname(path)))
        if not m:
            continue
        groups = defaultdict(list)
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                # failed NDR search (the app forwarded nothing): see no-drop-zip.py
                if (_f(r, "drop_rate_percentage") or 0) > NDR_FAILED_DROP_PCT:
                    continue
                groups[(r["policy"], int(r["queue"]))].append(r)
        data[m.group(1)][m.group(2)] = groups
    return data


def stat(rows, metric):
    vals = [v for v in (METRICS[metric][1](r) for r in rows) if v is not None]
    if not vals:
        return None
    return statistics.mean(vals), statistics.stdev(vals) if len(vals) > 1 else 0.0


def series(groups, policy, metric):
    xs, ys, es = [], [], []
    for (p, q), rows in sorted(groups.items(), key=lambda kv: kv[0][1]):
        if p != policy:
            continue
        s = stat(rows, metric)
        if s:
            xs.append(q)
            ys.append(s[0])
            es.append(s[1])
    return xs, ys, es


def draw(ax, xs, ys, es, style):
    if not xs:
        return
    lo = [y - e for y, e in zip(ys, es)]
    hi = [y + e for y, e in zip(ys, es)]
    ax.fill_between(xs, lo, hi, color=style["color"], alpha=0.15, lw=0)
    ax.plot(xs, ys, color=style["color"], lw=1.5, dashes=style["dash"])
    ax.plot(xs, ys, marker=style["marker"], ms=4, color=style["color"], mec="white",
            mew=0.6, ls="none", zorder=3)


def queue_axis(ax, queues, label=True):
    ax.set_xscale("log", base=2)
    ax.xaxis.set_major_locator(FixedLocator(queues))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(
        FuncFormatter(lambda v, _: f"{int(v) // 1024}k" if v >= 1024 else f"{int(v)}"))
    ax.tick_params(axis="x", labelsize=7)
    ax.set_xlabel("RX queues" if label else "")


def legend(fig, labels, y=1.0):
    handles = [plt.Line2D([], [], color=s["color"], marker=s["marker"], ms=5, lw=1.5,
                          dashes=s["dash"], label=l) for l, s in labels]
    fig.legend(handles=handles, loc="upper center", ncol=len(handles),
               bbox_to_anchor=(0.5, y), fontsize=8, handlelength=3)


def save(fig, out_dir, name):
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out_dir, f"{name}.{ext}"), dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  {name}.pdf/.png")


# A "line set" maps a legend label and style to (groups per skew, policy):
# single core plots one line per policy, multicore one line per core count.

def single_lines(data, app):
    per_skew = data.get(app, {})
    return [(p, STYLES[i], {s: (g, p) for s, g in per_skew.items()})
            for i, p in enumerate(POLICIES)
            if any(pp == p for g in per_skew.values() for pp, _ in g)]


def multicore_lines(data, app, mode):
    lines = []
    for i, c in enumerate(CORES):
        exp = app if c == 1 else f"{app}-{mode}{c}"
        per_skew = data.get(exp, {})
        if per_skew:
            label = "1 core" if c == 1 else f"{c} cores"
            lines.append((label, STYLES[i], {s: (g, "aggressive+penalty") for s, g in per_skew.items()}))
    return lines


def plot_metric(lines, metric, title, out_dir, name):
    skews = [s for s in SKEWS if any(s in src for _, _, src in lines)]
    if not skews:
        return
    fig, axes = plt.subplots(1, len(skews), figsize=(3.4 * len(skews), 2.7), squeeze=False)
    queues = set()
    any_data = False
    for ax, skew in zip(axes[0], skews):
        for _, style, src in lines:
            if skew not in src:
                continue
            xs, ys, es = series(*src[skew], metric)
            any_data |= bool(xs)
            queues.update(xs)
            draw(ax, xs, ys, es, style)
        ax.set_title(SKEWS[skew])
    if not any_data:
        plt.close(fig)
        return
    for ax in axes[0]:
        queue_axis(ax, sorted(queues))
        ax.set_ylim(bottom=0)
    axes[0][0].set_ylabel(METRICS[metric][0])
    fig.tight_layout(rect=(0, 0, 1, 0.84))
    legend(fig, [(l, s) for l, s, _ in lines], y=0.97)
    fig.suptitle(title, y=1.02, fontsize=9, color=INK)
    save(fig, out_dir, name)


def plot_overview(lines, metrics, title, out_dir, name):
    skews = [s for s in SKEWS if any(s in src for _, _, src in lines)]
    if not skews:
        return
    fig, axes = plt.subplots(len(metrics), len(skews), figsize=(3.2 * len(skews), 1.9 * len(metrics) + 0.6),
                             sharex=True, squeeze=False)
    queues = set()
    for row, metric in enumerate(metrics):
        for col, skew in enumerate(skews):
            ax = axes[row][col]
            for _, style, src in lines:
                if skew in src:
                    xs, ys, es = series(*src[skew], metric)
                    queues.update(xs)
                    draw(ax, xs, ys, es, style)
            ax.set_ylim(bottom=0)
            if row == 0:
                ax.set_title(SKEWS[skew])
            if col == 0:
                ax.set_ylabel(METRICS[metric][0], fontsize=8)
    for row in axes:
        for ax in row:
            queue_axis(ax, sorted(queues), label=ax in axes[-1])
    legend(fig, [(l, s) for l, s, _ in lines], y=1.0)
    fig.suptitle(title, y=1.015, fontsize=9, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.985))
    save(fig, out_dir, name)


def plot_shared_vs_partitioned(data, app, out_dir, queue=64, cores=2):
    modes = [("Partitioned", f"{app}-partitioned{cores}", STYLES[0]),
             ("Shared", f"{app}-shared{cores}", STYLES[1])]
    skews = [s for s in SKEWS if any(s in data.get(e, {}) for _, e, _ in modes)]
    if not skews:
        return
    metrics = ["throughput", "cycles", "l1_misses", "dram_bytes"]
    fig, axes = plt.subplots(1, len(metrics), figsize=(2.8 * len(metrics), 2.6))
    width = 0.38
    for ax, metric in zip(axes, metrics):
        for i, (label, exp, style) in enumerate(modes):
            for j, skew in enumerate(skews):
                rows = data.get(exp, {}).get(skew, {}).get(("aggressive+penalty", queue), [])
                s = stat(rows, metric) if rows else None
                if not s:
                    continue
                x = j + (i - 0.5) * width
                ax.bar(x, s[0], width - 0.04, color=style["color"], edgecolor="white", lw=0)
                ax.errorbar(x, s[0], yerr=s[1], color=INK_2, lw=0.8, capsize=2)
        ax.set_xticks(range(len(skews)))
        ax.set_xticklabels([SKEWS[s].split(" (")[0] for s in skews], fontsize=7)
        ax.set_ylabel(METRICS[metric][0], fontsize=8)
        ax.grid(axis="x", visible=False)
    handles = [plt.Rectangle((0, 0), 1, 1, color=s["color"], label=l) for l, _, s in modes]
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    fig.legend(handles=handles, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 0.97), fontsize=8)
    fig.suptitle(f"{app}: {queue} queues, {cores} cores, aggressive+penalty", y=1.02, fontsize=9, color=INK)
    save(fig, out_dir, f"{app}_shared_vs_partitioned")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", default=os.path.join(HERE, "results", "ndr"),
                        help="directory holding the results_* folders")
    parser.add_argument("--out", default=os.path.join(HERE, "plots", "ndr"))
    parser.add_argument("--apps", nargs="+", default=["nf-chain", "mica"])
    args = parser.parse_args()

    data = load(args.results)
    os.makedirs(args.out, exist_ok=True)
    print(f"experiments: {', '.join(sorted(data)) or 'none'}")

    for app in args.apps:
        lines = single_lines(data, app)
        if lines:
            for metric in METRICS:
                plot_metric(lines, metric, f"{app}, 1 core", args.out, f"{app}_{metric}")
            plot_overview(lines, list(METRICS), f"{app}, 1 core", args.out, f"{app}_overview")
        for mode in ("shared", "partitioned"):
            lines = multicore_lines(data, app, mode)
            if len(lines) < 2:
                continue
            title = f"{app}, {mode.capitalize()} + aggressive+penalty"
            metrics = [m for m, (_, _, single) in METRICS.items() if not single]
            for metric in metrics:
                plot_metric(lines, metric, title, args.out, f"{app}-{mode}_{metric}")
            plot_overview(lines, metrics, title, args.out, f"{app}-{mode}_overview")
        plot_shared_vs_partitioned(data, app, args.out)
    print(f"Plots in {args.out}/")


if __name__ == "__main__":
    main()
