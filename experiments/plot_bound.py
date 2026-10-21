"""
Single-core policies against the throughput bound of a perfect queue-selection
policy, from the results of no-drop-zip.py (it does not touch plot_ndr.py or
plots/ndr/).

  python3 plot_bound.py                 # nf-chain and mica
  python3 plot_bound.py --apps mica
  python3 plot_bound.py --apps mica-fifo-nocopy --bound seq-trex2   # another bound experiment

The bound is experiment <app>-seq-ring8192: the same Zipf flows sent one after
the other (sequential-profile.py) on 1 queue, so every burst holds one flow,
with the largest RX ring the QDMA accepts (8192). <app>-seq, the same with
the default 1024-descriptor ring, is not drawn: there the ring rather than the
processing limits the zero-loss rate of nf-chain. When the search of the bound
ended within GENERATOR_LIMITED_ITERATIONS trials, it lost packets only at the
first trial, the highest rate one TRex core sends (the sequential profile runs
on one core): the bound is then that rate, a lower bound of the real one, and
its label reads "theoretical max ≥". Writes PDF and PNG into plots/bound/:
  <app>_bound_<metric>   one panel per trace: the policies against the queues
                         (baseline called "round robin" here) and horizontal
                         lines, "theoretical max" (8192-descriptor ring) and
                         "baseline" (round robin on 1 queue, the normal trace); a
                         line is left out until its runs exist
  <app>_bound_overview   the same, every metric in one grid
Lines are the mean over repetitions, bands ±1 standard deviation, as in
plot_ndr.py.
"""

import argparse
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import plot_ndr as pn  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


BOUND_LINES = (("theoretical max", "seq-ring8192", dict(color=pn.INK, ls=(0, (5, 2)), lw=1.2)),
               ("baseline", "rr1", dict(color="#8a8a8a", ls=(0, (1, 1.5)), lw=1.4)))
GENERATOR_LIMITED_ITERATIONS = 3


BOUND = "seq-ring8192"  # --bound: experiment <app>-<BOUND> drawn as "theoretical max"


def bound_levels(data, app, skew, metric):
    """(label, (mean, std), style) of the two horizontal lines of a panel."""
    out = []
    for label, key, style in BOUND_LINES:
        key = BOUND if key == "seq-ring8192" else key
        if key != "rr1":
            rows = data.get(f"{app}-{key}", {}).get(skew, {}).get(("baseline", 1), [])
        else:
            rows = data.get(app, {}).get(skew, {}).get(("baseline", 1), [])
        s = pn.stat(rows, metric) if rows else None
        if s:
            if key != "rr1" and all(int(r["iterations"]) <= GENERATOR_LIMITED_ITERATIONS for r in rows):
                label += " ≥"
            out.append((label, s, style))
    return out


def draw_levels(ax, levels):
    for label, (y, e), style in levels:
        ax.axhline(y, **style, zorder=2)
        if e:
            ax.axhspan(y - e, y + e, color=style["color"], alpha=0.08, lw=0)
        ax.annotate(label, xy=(1.0, y), xycoords=("axes fraction", "data"), xytext=(-2, 2),
                    textcoords="offset points", ha="right", va="bottom", fontsize=6.5, color=style["color"])


def bound_legend(fig, lines, y):
    handles = [plt.Line2D([], [], color=s["color"], marker=s["marker"], ms=5, lw=1.5, dashes=s["dash"], label=l)
               for l, s, _ in lines]
    handles += [plt.Line2D([], [], label=l, **st) for l, _, st in BOUND_LINES]
    fig.legend(handles=handles, loc="upper center", ncol=len(handles), bbox_to_anchor=(0.5, y),
               fontsize=8, handlelength=3)


def plot_bound(data, app, out_dir):
    """Policies against the bound of a perfect policy (see the module docstring)."""
    seq = {**data.get(f"{app}-seq", {}), **data.get(f"{app}-{BOUND}", {})}
    if not seq:
        return
    lines = [("round robin" if l == "baseline" else l, st, src) for l, st, src in pn.single_lines(data, app)]
    skews = [s for s in pn.SKEWS if s in seq or any(s in src for _, _, src in lines)]
    for metric in list(pn.METRICS) + ["overview"]:
        metrics = list(pn.METRICS) if metric == "overview" else [metric]
        fig, axes = plt.subplots(len(metrics), len(skews), squeeze=False,
                                 figsize=(3.4 * len(skews), 2.7 if len(metrics) == 1 else 1.9 * len(metrics) + 0.6),
                                 sharex=True)
        queues = {1}
        for row, m in enumerate(metrics):
            for col, skew in enumerate(skews):
                ax = axes[row][col]
                for _, style, src in lines:
                    if skew in src:
                        xs, ys, es = pn.series(*src[skew], m)
                        queues.update(xs)
                        pn.draw(ax, xs, ys, es, style)
                draw_levels(ax, bound_levels(data, app, skew, m))
                ax.set_ylim(bottom=0)
                if row == 0:
                    ax.set_title(pn.SKEWS[skew])
                if col == 0:
                    ax.set_ylabel(pn.METRICS[m][0], fontsize=8)
        for r in axes:
            for ax in r:
                pn.queue_axis(ax, sorted(queues), label=ax in axes[-1])
        if len(metrics) == 1:
            fig.tight_layout(rect=(0, 0, 1, 0.84))
            bound_legend(fig, lines, 0.97)
            fig.suptitle(f"{app}, 1 core: policies against the bound of a perfect policy", y=1.02, fontsize=9,
                         color=pn.INK)
        else:
            bound_legend(fig, lines, 1.0)
            fig.suptitle(f"{app}, 1 core: policies against the bound of a perfect policy", y=1.015, fontsize=9,
                         color=pn.INK)
            fig.tight_layout(rect=(0, 0, 1, 0.985))
        pn.save(fig, out_dir, f"{app}_bound_{metric}")


def main():
    global BOUND
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", default=os.path.join(HERE, "results", "ndr"),
                        help="directory holding the results_* folders")
    parser.add_argument("--out", default=os.path.join(HERE, "plots", "bound"))
    parser.add_argument("--apps", nargs="+", default=["nf-chain", "mica"])
    parser.add_argument("--bound", default=BOUND,
                        help="suffix of the experiment drawn as theoretical max (default %(default)s)")
    args = parser.parse_args()
    BOUND = args.bound
    data = pn.load(args.results)
    os.makedirs(args.out, exist_ok=True)
    for app in args.apps:
        if f"{app}-seq" not in data and f"{app}-{BOUND}" not in data:
            print(f"{app}: no results_{app}-seq{{,-{BOUND}}}_skew* yet")
            continue
        plot_bound(data, app, args.out)
    print(f"Plots in {args.out}/")


if __name__ == "__main__":
    main()
