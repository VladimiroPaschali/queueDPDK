"""
Plots for the latency campaign of latency.py.

  python3 plot_latency.py                                   # default results directory
  python3 plot_latency.py results/latency/results_latency_nf-chain_skew0.9 --metric p50 --rate 1.5

Writes PDF and PNG into plots/latency/ (--out):
  latency_vs_rate_<metric>      one panel per queue count, latency vs offered rate
  latency_vs_queues_<metric>_<rate>Mpps
                                latency vs number of queues at one offered rate
  latency_vs_queues_grid_<metric>
                                the same, one panel per rate (--panel-rates)
  zero_loss_rate                highest offered rate without loss vs number of queues
  pareto_<metric>               throughput against latency, one panel per policy:
                                every queue count at its highest zero-loss rate,
                                with the Pareto frontier (counts that no other
                                count beats on both axes); dominated counts hollow
  pareto_all_<metric>           every policy together, frontier over all of them
                                (pareto for metric p99 and max, from latency_summary.csv)

Points that are not zero-loss (loss > 0.1%) are drawn with hollow markers and
dashed segments: their latency includes queueing from an overloaded core.
"""

import argparse
import csv
import os
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# Fixed order: a policy keeps its color, marker and dash in every figure.
# Colors are slots 1-4 of the validated reference palette; marker and dash
# are a second encoding, since aqua and yellow are under 3:1 on white.
POLICIES = {
    "baseline":           dict(color="#2a78d6", marker="o", dash=(None, None)),
    "aggressive":         dict(color="#eb6834", marker="s", dash=(4, 2)),
    "latency":            dict(color="#1baf7a", marker="^", dash=(1, 2)),
    "latency+aggressive": dict(color="#eda100", marker="D", dash=(6, 2, 1, 2)),
}
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
PARETO_METRICS = {"p99": "p99 latency (µs)", "max": "max latency (µs)"}

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


def load(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append({
                "queues": int(r["queues"]),
                "policy": r["policy"],
                "rate": float(r["rate_mpps"]),
                "zero_loss": r["zero_loss"] == "True",
                **{m: float(r[m]) for m in ("p50", "p90", "p95", "p99", "loss_pct")},
            })
    # average repetitions of the same point
    groups = defaultdict(list)
    for r in rows:
        groups[(r["queues"], r["policy"], r["rate"])].append(r)
    out = []
    for (q, p, rate), rs in groups.items():
        avg = {m: sum(r[m] for r in rs) / len(rs) for m in ("p50", "p90", "p95", "p99", "loss_pct")}
        out.append({"queues": q, "policy": p, "rate": rate,
                    "zero_loss": all(r["zero_loss"] for r in rs), **avg})
    return out


def series(rows, **match):
    rs = [r for r in rows if all(r[k] == v for k, v in match.items())]
    return sorted(rs, key=lambda r: (r["rate"], r["queues"]))


def draw(ax, xs, ys, ok, policy):
    """Line with solid markers on zero-loss points, hollow and dashed past them."""
    st = POLICIES[policy]
    for i in range(len(xs) - 1):
        lossy = not (ok[i] and ok[i + 1])
        ax.plot(xs[i:i + 2], ys[i:i + 2], color=st["color"], lw=1.5,
                dashes=(2, 2) if lossy else st["dash"], alpha=0.55 if lossy else 1)
    for x, y, good in zip(xs, ys, ok):
        ax.plot(x, y, marker=st["marker"], ms=5, color=st["color"],
                mfc=st["color"] if good else "white", mec=st["color"], mew=1.2,
                alpha=1 if good else 0.7, ls="none")


def last_zero_loss(ok):
    """Index of the last point before the first loss, or None."""
    last = None
    for i, good in enumerate(ok):
        if not good:
            break
        last = i
    return last


def draw_lines(ax, xs, ys, ok, policy):
    """Lines only, with one marker on the last zero-loss point."""
    st = POLICIES[policy]
    last = last_zero_loss(ok)
    split = -1 if last is None else last
    if split >= 0:
        ax.plot(xs[:split + 1], ys[:split + 1], color=st["color"], lw=1.5, dashes=st["dash"])
    if split < len(xs) - 1:
        ax.plot(xs[max(split, 0):], ys[max(split, 0):], color=st["color"], lw=1.2,
                dashes=(2, 2), alpha=0.45)
    if last is not None:
        ax.plot(xs[last], ys[last], marker=st["marker"], ms=6, color=st["color"],
                mec="white", mew=0.8, ls="none", zorder=3)


def legend(fig, extra=True, ncol=None, lossy_label="loss > 0.1%", lossy_marker=True):
    handles = [plt.Line2D([], [], color=s["color"], marker=s["marker"], ms=5, lw=1.5,
                          dashes=s["dash"], label=p) for p, s in POLICIES.items()]
    if extra:
        handles.append(plt.Line2D([], [], color=INK_2, marker="o" if lossy_marker else None,
                                  mfc="white", ms=5, lw=1.2, dashes=(2, 2), alpha=0.6,
                                  label=lossy_label))
    fig.legend(handles=handles, loc="upper center", ncol=ncol or len(handles),
               bbox_to_anchor=(0.5, 1.0), fontsize=8, handlelength=3)


def save(fig, out_dir, name):
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out_dir, f"{name}.{ext}"), dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  {name}.pdf/.png")


def queue_axis(ax, queues):
    ax.set_xscale("log", base=2)
    ax.xaxis.set_major_locator(FixedLocator(queues))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(
        FuncFormatter(lambda v, _: f"{int(v) // 1024}k" if v >= 1024 else f"{int(v)}"))
    ax.set_xlabel("RX queues")


def plot_vs_rate(rows, metric, out_dir):
    queues = sorted({r["queues"] for r in rows})
    ncols = 4
    nrows = (len(queues) + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(11, 2.3 * nrows + 0.6),
                             sharex=True, sharey=True, squeeze=False)
    for ax, q in zip(axes.flat, queues):
        for p in POLICIES:
            s = series(rows, queues=q, policy=p)
            if s:
                draw_lines(ax, [r["rate"] for r in s], [r[metric] for r in s],
                           [r["zero_loss"] for r in s], p)
        ax.set_title(f"{q} queue{'s' if q > 1 else ''}")
        ax.set_yscale("log")
    for ax in axes.flat[len(queues):]:
        ax.set_visible(False)
    for ax in axes[-1]:
        ax.set_xlabel("Offered load (Mpps)")
    for ax in axes[:, 0]:
        ax.set_ylabel(f"{metric} latency (µs, log)")
    legend(fig, lossy_label="after the first loss", lossy_marker=False)
    fig.text(0.5, 0.945, "Marker: highest offered load without loss", ha="center",
             fontsize=8, color=INK_2)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    save(fig, out_dir, f"latency_vs_rate_{metric}")


def plot_vs_queues(rows, metric, rate, out_dir):
    rows_at = [r for r in rows if abs(r["rate"] - rate) < 1e-6]
    if not rows_at:
        print(f"  no points at {rate} Mpps, skipping latency_vs_queues")
        return
    queues = sorted({r["queues"] for r in rows_at})
    fig, ax = plt.subplots(figsize=(5.5, 3.4))
    for p in POLICIES:
        s = sorted(series(rows_at, policy=p), key=lambda r: r["queues"])
        if not s:
            continue
        xs, ys = [r["queues"] for r in s], [r[metric] for r in s]
        draw(ax, xs, ys, [r["zero_loss"] for r in s], p)
    queue_axis(ax, queues)
    ax.set_yscale("log")
    ax.set_ylabel(f"{metric} latency (µs, log)")
    ax.set_title(f"Probe {metric} latency at {rate:g} Mpps")
    ax.margins(x=0.02)
    legend(fig, ncol=3)
    fig.tight_layout(rect=(0, 0, 1, 0.84))
    save(fig, out_dir, f"latency_vs_queues_{metric}_{rate:g}Mpps")


def draw_by_loss(ax, xs, ys, ok, policy):
    """Solid line and small markers between zero-loss points, faded dashes elsewhere."""
    st = POLICIES[policy]
    for i in range(len(xs) - 1):
        good = ok[i] and ok[i + 1]
        ax.plot(xs[i:i + 2], ys[i:i + 2], color=st["color"],
                lw=1.5 if good else 1.2, dashes=st["dash"] if good else (2, 2),
                alpha=1 if good else 0.45)
    gx = [x for x, g in zip(xs, ok) if g]
    gy = [y for y, g in zip(ys, ok) if g]
    ax.plot(gx, gy, marker=st["marker"], ms=4, color=st["color"], mec="white",
            mew=0.6, ls="none", zorder=3)


def plot_vs_queues_grid(rows, metric, rates, out_dir):
    available = sorted({r["rate"] for r in rows})
    rates = [r for r in rates if any(abs(r - a) < 1e-6 for a in available)]
    if not rates:
        print("  none of the panel rates was measured, skipping latency_vs_queues_grid")
        return
    queues = sorted({r["queues"] for r in rows})
    ncols = 4
    nrows = (len(rates) + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(11, 2.3 * nrows + 0.6),
                             sharex=True, sharey=True, squeeze=False)
    for ax, rate in zip(axes.flat, rates):
        rows_at = [r for r in rows if abs(r["rate"] - rate) < 1e-6]
        for p in POLICIES:
            s = sorted(series(rows_at, policy=p), key=lambda r: r["queues"])
            if s:
                draw_by_loss(ax, [r["queues"] for r in s], [r[metric] for r in s],
                             [r["zero_loss"] for r in s], p)
        ax.set_title(f"{rate:g} Mpps")
        ax.set_yscale("log")
        queue_axis(ax, queues)
        ax.set_xlabel("")
        ax.tick_params(axis="x", labelsize=7)
    for ax in axes.flat[len(rates):]:
        ax.set_visible(False)
    for ax in axes[-1]:
        ax.set_xlabel("RX queues")
    for ax in axes[:, 0]:
        ax.set_ylabel(f"{metric} latency (µs, log)")
    legend(fig, lossy_label="loss > 0.1%", lossy_marker=False)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    save(fig, out_dir, f"latency_vs_queues_grid_{metric}")


def plot_zero_loss(rows, out_dir):
    queues = sorted({r["queues"] for r in rows})
    fig, ax = plt.subplots(figsize=(5.5, 3.4))
    for p in POLICIES:
        xs, ys = [], []
        for q in queues:
            s = series(rows, queues=q, policy=p)
            # highest rate below which every point is zero-loss
            best = 0.0
            for r in s:
                if not r["zero_loss"]:
                    break
                best = r["rate"]
            if s:
                xs.append(q)
                ys.append(best)
        draw(ax, xs, ys, [True] * len(xs), p)
    queue_axis(ax, queues)
    ax.set_ylim(bottom=0)
    ax.set_ylabel("Zero-loss rate (Mpps)")
    ax.set_title("Highest offered load without loss")
    legend(fig, extra=False, ncol=2)
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    save(fig, out_dir, "zero_loss_rate")


def load_pareto(path):
    """{policy: {queues: (rate Mpps, {metric: µs})}} at the highest zero-loss rate."""
    best = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["zero_loss"] != "True":
                continue
            key = (r["policy"], int(r["queues"]))
            rate = float(r["rate_mpps"])
            if key not in best or rate > best[key][0]:
                best[key] = (rate, {m: float(r[f"{m}_mean"]) for m in PARETO_METRICS})
    out = {}
    for (p, q), v in best.items():
        out.setdefault(p, {})[q] = v
    return out


def frontier(points):
    """Points (rate, latency, tag) not dominated by another one, by rate."""
    pts = sorted(points, key=lambda x: (-x[0], x[1]))
    front, low = [], float("inf")
    for p in pts:
        if p[1] < low:
            front.append(p)
            low = p[1]
    return sorted(front)


def place_labels(ax, labels):
    """Annotate (x, y, text, color) next to each point: right above, right below,
    left above, left below, then further away, taking the first spot that does
    not overlap a label already placed (display coordinates)."""
    ax.figure.canvas.draw()
    placed = []
    for x, y, text, color in sorted(labels, key=lambda l: (-l[1], l[0])):
        px, py = ax.transData.transform((x, y))
        w, h = 4.0 * len(text) + 4, 8
        spots = [(4, 3), (4, -10), (-4 - w, 3), (-4 - w, -10), (4, 12), (4, -19), (-4 - w, 12), (-4 - w, -19)]
        for dx, dy in spots:
            box = (px + dx, py + dy, px + dx + w, py + dy + h)
            if not any(box[0] < q[2] and q[0] < box[2] and box[1] < q[3] and q[1] < box[3] for q in placed):
                break
        placed.append(box)
        ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points", fontsize=6.5, color=color)


def plot_pareto(data, metric, title, out):
    policies = [p for p in POLICIES if p in data]
    fig, axes = plt.subplots(1, len(policies), figsize=(3.3 * len(policies), 3.0), sharey=True, squeeze=False)
    for ax, policy in zip(axes[0], policies):
        style = POLICIES[policy]
        pts = [(rate, lat[metric], q) for q, (rate, lat) in data[policy].items()]
        front = frontier(pts)
        labels = []
        on = {p[2] for p in front}
        ax.step([p[0] for p in front], [p[1] for p in front], where="post", color=style["color"], lw=1.2,
                alpha=0.7)
        for rate, lat, q in pts:
            ax.plot(rate, lat, marker=style["marker"], ms=6, ls="none", color=style["color"],
                    mfc=style["color"] if q in on else "white", mec=style["color"], mew=1.2, zorder=3)
            labels.append((rate, lat, f"{q}q", INK if q in on else INK_2))
        ax.set_title(policy)
        ax.set_yscale("log")
        ax.set_xlabel("zero-loss throughput (Mpps)")
        place_labels(ax, labels)
    axes[0][0].set_ylabel(PARETO_METRICS[metric])
    handles = [plt.Line2D([], [], marker="o", ls="none", color=INK_2, mfc=INK_2, label="Pareto frontier"),
               plt.Line2D([], [], marker="o", ls="none", color=INK_2, mfc="white", label="dominated")]
    fig.legend(handles=handles, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.02), fontsize=8)
    fig.suptitle(title, y=1.08, fontsize=9, color=INK)
    fig.tight_layout()
    save(fig, out, f"pareto_{metric}")


def plot_pareto_all(data, metric, title, out):
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    pts = []
    for policy in [p for p in POLICIES if p in data]:
        style = POLICIES[policy]
        for q, (rate, lat) in data[policy].items():
            pts.append((rate, lat[metric], (policy, q)))
            ax.plot(rate, lat[metric], marker=style["marker"], ms=5, ls="none", color=style["color"],
                    mfc="white", mew=1.0, alpha=0.8)
    front = frontier(pts)
    ax.step([p[0] for p in front], [p[1] for p in front], where="post", color=INK, lw=1.2, zorder=2)
    for rate, lat, (policy, q) in front:
        style = POLICIES[policy]
        ax.plot(rate, lat, marker=style["marker"], ms=7, ls="none", color=style["color"], zorder=3)
    ax.set_yscale("log")
    ax.set_xlabel("zero-loss throughput (Mpps)")
    ax.set_ylabel(PARETO_METRICS[metric])
    handles = [plt.Line2D([], [], marker=s["marker"], ls="none", color=s["color"], label=p)
               for p, s in POLICIES.items() if p in data]
    handles.append(plt.Line2D([], [], color=INK, lw=1.2, label="Pareto frontier (filled)"))
    ax.legend(handles=handles, fontsize=7, loc="upper left")
    ax.set_title(title)
    fig.tight_layout()
    place_labels(ax, [(rate, lat, f"{policy}, {q}q", INK) for rate, lat, (policy, q) in front])
    save(fig, out, f"pareto_all_{metric}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="?",
                        default=os.path.join(HERE, "results", "latency", "results_latency_nf-chain_skew0.6"),
                        help="results directory written by latency.py")
    parser.add_argument("--out", default=os.path.join(HERE, "plots", "latency"))
    parser.add_argument("--metric", default="p99", choices=["p50", "p90", "p95", "p99"])
    parser.add_argument("--rate", type=float, default=2.0,
                        help="offered rate (Mpps) for latency_vs_queues")
    parser.add_argument("--panel-rates", type=float, nargs="+",
                        default=[round(1.0 + 0.2 * i, 1) for i in range(16)],
                        help="rates (Mpps), one panel each, for latency_vs_queues_grid")
    args = parser.parse_args()

    results = args.results if os.path.isabs(args.results) else os.path.join(HERE, args.results)
    rows = load(os.path.join(results, "latency_results.csv"))
    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    print(f"{len(rows)} points from {results}")

    plot_vs_rate(rows, args.metric, out_dir)
    plot_vs_queues(rows, args.metric, args.rate, out_dir)
    plot_vs_queues_grid(rows, args.metric, args.panel_rates, out_dir)
    plot_zero_loss(rows, out_dir)
    pareto = load_pareto(os.path.join(results, "latency_summary.csv"))
    skew = os.path.basename(os.path.normpath(results)).rsplit("skew", 1)[-1]
    for metric in PARETO_METRICS:
        title = f"nf-chain, skew {skew}: throughput against {metric} latency per queue count"
        plot_pareto(pareto, metric, title, out_dir)
        plot_pareto_all(pareto, metric, title, out_dir)
    for policy, by_q in pareto.items():
        front = frontier([(rate, lat["p99"], q) for q, (rate, lat) in by_q.items()])
        print(f"{policy:20} p99 frontier: " + ", ".join(f"{q}q {r:.1f} Mpps {l:.0f} µs" for r, l, q in front))
    print(f"Plots in {out_dir}/")


if __name__ == "__main__":
    main()
