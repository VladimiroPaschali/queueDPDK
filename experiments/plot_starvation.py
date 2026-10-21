"""
Plots for the starvation / FCT campaign of starvation.py.

  python3 plot_starvation.py                          # results/starvation/
  python3 plot_starvation.py results/starvation --skew 0.6 --rate 2
  python3 plot_starvation.py results/starvation/ndr --ndr  # starvation.py --ndr

Writes PDF and PNG into plots/starvation/ (--out):
  service_gap_<metric>    max / p99.9 service gap vs queues, one panel per skew x rate
  drain_gap_<metric>      the same for the drain gap
  queue_scatter_s<skew>_r<rate>
                          per-queue max service gap vs queue load, with the RX ring
                          overflow curve 1024 / load
  delay_ccdf_r<rate>      CCDF of the probes' one-way delay, one panel per skew x queues
  fct_s<skew>             p50 / p99 FCT vs flow size, one panel per rate x queues
  fct_slowdown_s<skew>    the same as slowdown (FCT / (TX span + min one-way delay))
  rank_delay_s<skew>_r<rate>
                          max / p99 one-way delay of the probes vs the Zipf rank they sit on
  loss                    TRex loss vs queues
  ndr_rate                with --ndr: the zero-loss rate of every point vs queues

With --ndr every point ran at its own zero-loss rate: the plots have one rate
column ("0.1%-loss rate", files *_rndr) instead of one per --rates value.

Points with loss > 0.1% (the NDR tolerance) are drawn hollow.
"""

import argparse
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# Same slots of the reference palette as plot_latency.py
POLICIES = {
    "baseline":           dict(color="#2a78d6", marker="o", dash=(None, None)),
    "aggressive":         dict(color="#eb6834", marker="s", dash=(4, 2)),
    "penalty":            dict(color="#1baf7a", marker="^", dash=(1, 2)),
    "aggressive+penalty": dict(color="#eda100", marker="D", dash=(6, 2, 1, 2)),
}
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
LOSS_OK = 0.1
NDR = -1.0  # rate_mpps of every point with --ndr (each has its own rate, in rate_ndr)


def rate_title(r):
    return f"{LOSS_OK:g}%-loss rate" if r == NDR else f"{r:g} Mpps"


def rate_tag(r):
    return "ndr" if r == NDR else f"{r:g}"

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


def save(fig, out, name):
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out, f"{name}.{ext}"), bbox_inches="tight", dpi=160)
    plt.close(fig)
    print("wrote", os.path.join(out, name + ".png"))


def us_fmt(v, _):
    if v >= 1000:
        return f"{v / 1000:g} ms"
    return f"{v:g} µs" if v >= 1 else f"{v:g}"


def queue_axis(ax, queues):
    ax.set_xscale("log", base=2)
    ax.xaxis.set_major_locator(FixedLocator(queues))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v)}"))


def legend(fig, policies, y=1.02):
    handles = [plt.Line2D([], [], color=POLICIES[p]["color"], marker=POLICIES[p]["marker"],
                          dashes=POLICIES[p]["dash"], lw=1.5, ms=5, label=p) for p in policies]
    handles.append(plt.Line2D([], [], color=INK_2, marker="o", mfc="white", ls="none",
                              label=f"loss > {LOSS_OK}%"))
    fig.legend(handles=handles, loc="upper center", ncol=len(handles), bbox_to_anchor=(0.5, y))


def draw(ax, xs, ys, ok, policy):
    st = POLICIES[policy]
    ax.plot(xs, ys, color=st["color"], lw=1.4, dashes=st["dash"])
    for x, y, good in zip(xs, ys, ok):
        ax.plot(x, y, marker=st["marker"], ms=4.5, color=st["color"],
                mfc=st["color"] if good else "white", mec=st["color"], ls="none")


def grid_vs_queues(runs, metric, ylabel, out, name, log=True, fmt=us_fmt):
    skews, rates = sorted(runs["skew"].unique()), sorted(runs.rate_mpps.unique())
    queues = sorted(runs.queues.unique())
    policies = [p for p in POLICIES if p in set(runs.policy)]
    one = len(rates) == 1  # --ndr: the skews side by side, wide enough for the queue labels
    nr, nc = (1, len(skews)) if one else (len(skews), len(rates))
    fig, axes = plt.subplots(nr, nc, figsize=((3.6 if one else 2.6) * nc, 2.4 * nr),
                             sharex=True, sharey=True, squeeze=False)
    for i, s in enumerate(skews):
        for j, r in enumerate(rates):
            ax = axes[0][i] if one else axes[i][j]
            for p in policies:
                d = runs[(runs["skew"] == s) & (runs.rate_mpps == r) & (runs.policy == p)].sort_values("queues")
                if len(d):
                    draw(ax, d.queues, d[metric], d.loss_pct <= LOSS_OK, p)
            queue_axis(ax, queues)
            if log:
                ax.set_yscale("log")
                ax.yaxis.set_major_formatter(FuncFormatter(fmt))
            ax.set_title(f"skew {s:g}, {rate_title(r)}")
            if one or i == len(skews) - 1:
                ax.set_xlabel("RX queues")
            if (i if one else j) == 0:
                ax.set_ylabel(ylabel)
    legend(fig, policies, 1.1 if one else 1.02)
    fig.tight_layout()
    save(fig, out, name)


def queue_scatter(queues_df, runs, skew, rate, out):
    d = queues_df[(queues_df["skew"] == skew) & (queues_df.rate_mpps == rate) & (queues_df.polls >= 10)]
    if d.empty:
        return
    qs = [q for q in sorted(d.queues.unique()) if q > 1]
    policies = [p for p in POLICIES if p in set(d.policy)]
    fig, axes = plt.subplots(1, len(qs), figsize=(2.7 * len(qs), 2.6), sharey=True, squeeze=False)
    lo, hi = d.load_pps[d.load_pps > 0].min(), d.load_pps.max()
    for j, q in enumerate(qs):
        ax = axes[0][j]
        for p in policies:
            e = d[(d.queues == q) & (d.policy == p)]
            st = POLICIES[p]
            ax.scatter(e.load_pps, e.max_svc_us, s=7, color=st["color"], marker=st["marker"],
                       alpha=0.55, linewidths=0, label=p)
        x = np.logspace(np.log10(lo), np.log10(hi), 50)
        ax.plot(x, 1024 / x * 1e6, color=INK, lw=1, ls=":")
        ax.text(x[len(x) // 3], 1024 / x[len(x) // 3] * 1e6 * 1.3, "ring overflow", color=INK_2, fontsize=7)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_ylim(top=max(1e6, d.max_svc_us.max() * 3))  # 1 s: the overflow curve goes far above
        ax.yaxis.set_major_formatter(FuncFormatter(us_fmt))
        lossy = runs[(runs["skew"] == skew) & (runs.rate_mpps == rate) & (runs.queues == q) &
                     (runs.loss_pct > LOSS_OK)].policy.tolist()
        ax.set_title(f"{q} queues" + (f"\nloss: {', '.join(lossy)}" if lossy else ""))
        ax.set_xlabel("queue load (pkts/s)")
        if j == 0:
            ax.set_ylabel("max service gap")
    legend(fig, policies)
    fig.suptitle(f"skew {skew:g}, {rate_title(rate)}", y=1.08, fontsize=9)
    fig.tight_layout()
    save(fig, out, f"queue_scatter_s{skew:g}_r{rate_tag(rate)}")


def ccdf(ax, lo, n, **kw):
    order = np.argsort(lo)
    lo, n = np.asarray(lo)[order], np.asarray(n)[order]
    tot = n.sum()
    if tot == 0:
        return
    tail = 1 - np.cumsum(n) / tot + n / tot  # P(delay >= bin)
    ax.step(lo, tail, where="post", **kw)


def delay_ccdf(hist, runs, rate, out, kind="probe"):
    d = hist[(hist.rate_mpps == rate) & (hist.kind == kind)]
    if d.empty:
        return
    skews, qs = sorted(d["skew"].unique()), sorted(d.queues.unique())
    policies = [p for p in POLICIES if p in set(d.policy)]
    fig, axes = plt.subplots(len(skews), len(qs), figsize=(2.3 * len(qs), 2.0 * len(skews)),
                             sharex=True, sharey=True, squeeze=False)
    for i, s in enumerate(skews):
        for j, q in enumerate(qs):
            ax = axes[i][j]
            for p in policies:
                e = d[(d["skew"] == s) & (d.queues == q) & (d.policy == p)]
                st = POLICIES[p]
                lossy = runs[(runs["skew"] == s) & (runs.queues == q) & (runs.policy == p) &
                             (runs.rate_mpps == rate)].loss_pct.max() > LOSS_OK
                ccdf(ax, e.lo_us, e.n, color=st["color"], lw=1.3, dashes=st["dash"],
                     alpha=0.45 if lossy else 1)
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_ylim(1e-5, 1.2)
            ax.xaxis.set_major_formatter(FuncFormatter(us_fmt))
            ax.set_title(f"skew {s:g}, {q} queues")
            if i == len(skews) - 1:
                ax.set_xlabel("one-way delay − min")
            if j == 0:
                ax.set_ylabel("P(delay ≥ x)")
    legend(fig, policies)
    fig.suptitle(f"{kind} packets, {rate_title(rate)} (faded: loss > {LOSS_OK}%)", y=1.07, fontsize=9)
    fig.tight_layout()
    save(fig, out, f"delay_ccdf_{kind}_r{rate_tag(rate)}")


def fct_slowdown(fct, runs, skew, out, metric="slowdown"):
    d = fct[(fct["skew"] == skew) & (fct.lost == 0)]
    if d.empty:
        return
    rates, qs = sorted(d.rate_mpps.unique()), sorted(d.queues.unique())
    sizes = sorted(d["size"].unique())
    policies = [p for p in POLICIES if p in set(d.policy)]
    g = d.groupby(["rate_mpps", "queues", "policy", "size"])[metric]
    stats = pd.DataFrame({"p50": g.median(), "p99": g.quantile(0.99)}).reset_index()
    ideal = d.groupby("size").ideal_us.median()
    fig, axes = plt.subplots(len(rates), len(qs), figsize=(2.3 * len(qs), 2.0 * len(rates)),
                             sharex=True, sharey=True, squeeze=False)
    for i, r in enumerate(rates):
        for j, q in enumerate(qs):
            ax = axes[i][j]
            for p in policies:
                e = stats[(stats.rate_mpps == r) & (stats.queues == q) & (stats.policy == p)].sort_values("size")
                st = POLICIES[p]
                lossy = runs[(runs["skew"] == skew) & (runs.queues == q) & (runs.policy == p) &
                             (runs.rate_mpps == r)].loss_pct.max() > LOSS_OK
                ax.plot(e["size"], e.p99, color=st["color"], marker=st["marker"], ms=3.5, lw=1.3,
                        mfc="white" if lossy else st["color"])
                ax.plot(e["size"], e.p50, color=st["color"], lw=0.9, ls=":")
            if metric == "fct_us":
                ax.plot(ideal.index, ideal.values + 1, color=INK, lw=1, ls="--")
            ax.set_xscale("log", base=4)
            ax.xaxis.set_major_locator(FixedLocator(sizes))
            ax.xaxis.set_minor_locator(NullLocator())
            ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v)}"))
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(FuncFormatter(us_fmt if metric == "fct_us" else lambda v, _: f"{v:g}"))
            ax.set_title(f"{rate_title(r)}, {q} queues")
            if i == len(rates) - 1:
                ax.set_xlabel("flow size (packets)")
            if j == 0:
                ax.set_ylabel("FCT" if metric == "fct_us" else "FCT slowdown")
    legend(fig, policies)
    extra = ", ideal (dashed): TX span" if metric == "fct_us" else ""
    fig.suptitle(f"skew {skew:g}: p99 (solid) and p50 (dotted){extra}", y=1.07, fontsize=9)
    fig.tight_layout()
    save(fig, out, f"{'fct' if metric == 'fct_us' else 'fct_slowdown'}_s{skew:g}")


def fct_size_vs_queues(fct, runs, skew, rate, out, size=1024, stat="median"):
    """Median (or p99) FCT of the @size-packet bursts vs queues, over every
    burst (also those that lost packets), one line per policy."""
    d = fct[(fct["skew"] == skew) & (fct.rate_mpps == rate) & (fct["size"] == size)]
    if d.empty:
        return
    qs = sorted(d.queues.unique())
    pos = {q: i for i, q in enumerate(qs)}
    policies = [p for p in POLICIES if p in set(d.policy)]
    fig, ax = plt.subplots(figsize=(5.2, 3.0))
    ends = []
    for p in policies:
        e = d[d.policy == p]
        g = e.groupby("queues").fct_us
        med = (g.median() if stat == "median" else g.quantile(0.99)) / 1000
        lossy = e.groupby("queues").lost.apply(lambda s: (s > 0).mean())
        st = POLICIES[p]
        x = [pos[q] for q in med.index]
        ax.plot(x, med.values, color=st["color"], marker=st["marker"], ms=5, lw=1.6, dashes=st["dash"], label=p)
        ends.append((x[-1], med.values[-1], st["color"]))
        if rate == NDR:  # share of the bursts that lost packets, under every point
            for xi, q in zip(x, med.index):
                ax.annotate(f"{lossy[q] * 100:.0f}%", (xi, med[q]), xytext=(0, -11), textcoords="offset points",
                            ha="center", fontsize=6.5, color=INK_2)
    ideal = d.ideal_us.median() / 1000
    ax.axhline(ideal, color=INK_2, lw=0.8, ls="--")
    ax.text(len(qs) - 0.25, ideal * 1.04, f"ideale {ideal:.1f} ms (durata TX)".replace(".", ","), color=INK_2,
            fontsize=7.5, ha="right", va="bottom")
    ax.set_yscale("log")
    ax.set_ylim(bottom=ideal * 0.75)
    lo, hi = ax.get_ylim()
    ax.yaxis.set_major_locator(FixedLocator([1, 2, 5, 10, 20, 50, 100, 200, 500] if hi / lo > 3 else
                                            [4, 5, 6, 7, 8, 9, 10, 12, 15]))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.set_xticks(range(len(qs)), [str(q) for q in qs])
    ax.set_xlim(-0.4, len(qs) - 0.2)
    ax.set_xlabel("RX queues")
    ax.set_ylabel("FCT mediano (ms)" if stat == "median" else "FCT p99 (ms)")
    fig.canvas.draw()  # value at the end of every line, at least 9 points apart
    last = -1e9
    for x, y, color in sorted(ends, key=lambda e: e[1]):
        py = ax.transData.transform((x, y))[1] * 72 / fig.dpi
        dy = max(0.0, last + 9 - py)
        last = py + dy
        ax.annotate(f"{y:.1f} ms", (x, y), xytext=(8, dy), textcoords="offset points", va="center",
                    fontsize=7.5, color=color)
    pps = d["pps"].iloc[0] if "pps" in d else 200000
    where = "rate allo 0,1% di perdita" if rate == NDR else f"{rate:g} Mpps"
    ax.set_title(f"Flusso da {size} pacchetti a {pps / 1000:g} kpps, skew {skew:g}, {where}", fontsize=8.5)
    ax.legend(loc="upper left", fontsize=7.5)
    if rate == NDR:
        ax.text(0.99, 0.02, "sotto i punti: raffiche con perdite", transform=ax.transAxes, ha="right",
                fontsize=6.5, color=INK_2)
    fig.tight_layout()
    save(fig, out, f"fct{size}{'' if stat == 'median' else '_' + stat}_vs_queues_s{skew:g}_r{rate_tag(rate)}")


def rank_delay(flows, runs, skew, rate, out):
    """Probes sit on the 5-tuple of Zipf ranks 1..10000 and carry the TRex TX
    timestamp. (The sampled Zipf flows are not used: their delay comes from
    the seq and includes the TX jitter of TRex.)"""
    d = flows[(flows["skew"] == skew) & (flows.rate_mpps == rate) & (flows.kind == "probe")]
    if d.empty:
        return
    qs = sorted(d.queues.unique())
    policies = [p for p in POLICIES if p in set(d.policy)]
    fig, axes = plt.subplots(1, len(qs), figsize=(2.4 * len(qs), 2.3), sharey=True, squeeze=False)
    for j, q in enumerate(qs):
        ax = axes[0][j]
        for p in policies:
            e = d[(d.queues == q) & (d.policy == p)].sort_values("rank")
            st = POLICIES[p]
            lossy = runs[(runs["skew"] == skew) & (runs.rate_mpps == rate) & (runs.queues == q) &
                         (runs.policy == p)].loss_pct.max() > LOSS_OK
            ax.plot(e["rank"], e["max"], color=st["color"], lw=1.2, dashes=st["dash"],
                    marker=st["marker"], ms=3.5, mfc="white" if lossy else st["color"])
            ax.plot(e["rank"], e.p99, color=st["color"], lw=0.8, ls=":")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(FuncFormatter(us_fmt))
        ax.set_title(f"{q} queues")
        ax.set_xlabel("Zipf rank (1 = hottest)")
        if j == 0:
            ax.set_ylabel("one-way delay − min")
    legend(fig, policies)
    fig.suptitle(f"probes on Zipf ranks, skew {skew:g}, {rate_title(rate)}: max (solid), p99 (dotted)",
                 y=1.08, fontsize=9)
    fig.tight_layout()
    save(fig, out, f"rank_delay_s{skew:g}_r{rate_tag(rate)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="?", default=os.path.join(HERE, "results", "starvation"))
    parser.add_argument("--out", help="default plots/starvation, plots/starvation/ndr with --ndr")
    parser.add_argument("--ndr", action="store_true", help="results of starvation.py --ndr")
    parser.add_argument("--skew", type=float, default=0.6, help="skew of the per-queue / FCT / rank plots")
    parser.add_argument("--rate", type=float, default=2.0, help="rate of the per-queue / CCDF / rank plots")
    args = parser.parse_args()

    res = os.path.join(HERE, args.results) if not os.path.isabs(args.results) else args.results
    out = args.out or os.path.join(HERE, "plots", "starvation", "ndr" if args.ndr else "")
    os.makedirs(out, exist_ok=True)
    runs = pd.read_csv(os.path.join(res, "runs.csv"))
    queues_df = pd.read_csv(os.path.join(res, "queues.csv"))
    flows = pd.read_csv(os.path.join(res, "flows.csv"))
    fct = pd.read_csv(os.path.join(res, "fct.csv"))
    hist = pd.read_csv(os.path.join(res, "delayhist.csv"))
    if args.ndr:
        runs, queues_df, flows, fct, hist = (df.assign(rate_ndr=df.rate_mpps, rate_mpps=NDR)
                                             for df in (runs, queues_df, flows, fct, hist))
        grid_vs_queues(runs, "rate_ndr", "zero-loss rate (Mpps)", out, "ndr_rate", log=False)

    grid_vs_queues(runs, "svc_max_us", "max service gap", out, "service_gap_max")
    grid_vs_queues(runs, "svc_p999_us", "p99.9 service gap", out, "service_gap_p999")
    grid_vs_queues(runs, "drain_max_us", "max drain gap", out, "drain_gap_max")
    grid_vs_queues(runs, "drain_p999_us", "p99.9 drain gap", out, "drain_gap_p999")
    grid_vs_queues(runs.assign(loss_pct_plot=runs.loss_pct.clip(lower=1e-4)), "loss_pct_plot",
                   "loss (%)", out, "loss", fmt=lambda v, _: f"{v:g}")
    for s, r in ([(s, NDR) for s in sorted(runs["skew"].unique())] if args.ndr else [(args.skew, args.rate)]):
        for stat in ("median", "p99"):
            fct_size_vs_queues(fct, runs, s, r, out, stat=stat)
    for s in sorted(runs["skew"].unique()):
        fct_slowdown(fct, runs, s, out)
        fct_slowdown(fct, runs, s, out, "fct_us")
        for r in sorted(runs.rate_mpps.unique()):
            queue_scatter(queues_df, runs, s, r, out)
    for r in sorted(runs.rate_mpps.unique()):
        delay_ccdf(hist, runs, r, out, "probe")
        delay_ccdf(hist, runs, r, out, "fct")
    for s in sorted(runs["skew"].unique()):
        for r in sorted(runs.rate_mpps.unique()):
            rank_delay(flows, runs, s, r, out)


if __name__ == "__main__":
    main()
