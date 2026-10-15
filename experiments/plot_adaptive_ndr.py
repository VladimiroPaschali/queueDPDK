"""
Plots and comparison table for adaptive_ndr.py: zero-loss rate of the adaptive
controllers against the queue sweep (fixed queue counts), per number of flows.

  python3 plot_adaptive_ndr.py                       # results/adaptive/ndr_v2/

Writes PDF and PNG into plots/adaptive/<name of the results folder>/ (--out):
  sweep_<policy>     one panel per number of flows: zero-loss Mpps against the
                     fixed queue count (the sweep), with the point where each
                     controller lands (its queue count at the zero-loss rate,
                     its zero-loss rate); with 10000 flows the queue sweep of
                     no-drop-zip.py (nf-chain, NdrBench) is drawn in grey too
  summary            per policy, against the number of flows: zero-loss Mpps
                     (best fixed count, signal) and queues used (knee of the
                     sweep, signal)
and prints the comparison table, also saved as <results>/comparison.csv.

The knee is the fewest fixed queues within --knee percent of the best fixed
zero-loss rate: the target of a controller that wants the highest rate with
the fewest queues.
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
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SWEEP = "#2a78d6"
CONTROLLERS = {"signal": dict(color="#eb6834", marker="D")}
POLICIES = ["baseline", "aggressive", "penalty", "aggressive+penalty"]

plt.rcParams.update({
    "font.size": 9, "axes.edgecolor": INK_2, "axes.labelcolor": INK, "axes.titlesize": 9,
    "axes.titlecolor": INK, "xtick.color": INK_2, "ytick.color": INK_2,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.6, "legend.frameon": False,
})


def queue_axis(ax, queues):
    ax.set_xscale("log", base=2)
    ax.xaxis.set_major_locator(FixedLocator([q for q in queues if int(np.log2(q)) % 2 == 0] or queues))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1024:g}k" if v >= 1024 else f"{v:g}"))


def save(fig, out, name):
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out, f"{name}.{ext}"), bbox_inches="tight", dpi=160)
    plt.close(fig)
    print(f"  {name}.pdf/.png")


def load(results):
    df = pd.read_csv(os.path.join(results, "ndr.csv"))
    df["static_q"] = df["mode"].str.extract(r"static-(\d+)", expand=False).astype(float)
    num = ["ndr_mpps", "queues", "empty_pct", "full_pct", "busy"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df.groupby(["flows", "policy", "mode"], as_index=False).agg(
        ndr_mpps=("ndr_mpps", "mean"), ndr_std=("ndr_mpps", "std"), queues=("queues", "median"),
        static_q=("static_q", "first"), busy=("busy", "mean"), empty_pct=("empty_pct", "mean"),
        full_pct=("full_pct", "mean"))


def old_sweep(skew, policy):
    """Queue sweep of no-drop-zip.py (10000 flows), if present."""
    path = os.path.join(HERE, "results", "ndr", f"results_nf-chain_skew{skew:g}", "benchmark_results.csv")
    if not os.path.exists(path):
        return None
    r = pd.read_csv(path)
    r = r[(r["policy"] == policy) & (r["drop_rate_percentage"] <= 0.2)]
    return r.groupby("queue")["tx_pps"].mean() / 1e6 if len(r) else None


def comparison(df, knee_pct):
    rows = []
    for (flows, policy), g in df.groupby(["flows", "policy"]):
        st = g[g["static_q"].notna()].sort_values("static_q")
        row = dict(flows=flows, policy=policy)
        if len(st):
            best = st.loc[st["ndr_mpps"].idxmax()]
            knee = st[st["ndr_mpps"] >= best["ndr_mpps"] * (1 - knee_pct / 100)].iloc[0]
            row.update(best_q=int(best["static_q"]), best_mpps=best["ndr_mpps"],
                       knee_q=int(knee["static_q"]), knee_mpps=knee["ndr_mpps"])
        for c in CONTROLLERS:
            r = g[g["mode"] == c]
            if len(r):
                r = r.iloc[0]
                row[f"{c}_q"] = r["queues"]
                row[f"{c}_mpps"] = r["ndr_mpps"]
                if "best_mpps" in row:
                    row[f"{c}_vs_best_pct"] = (r["ndr_mpps"] / row["best_mpps"] - 1) * 100
        rows.append(row)
    return pd.DataFrame(rows)


def plot_sweeps(df, cmp, skew, out):
    for policy in [p for p in POLICIES if p in set(df["policy"])]:
        flows = sorted(df[df["policy"] == policy]["flows"].unique())
        fig, axes = plt.subplots(1, len(flows), figsize=(3.0 * len(flows), 2.7), squeeze=False, sharey=True)
        for ax, f in zip(axes[0], flows):
            g = df[(df["policy"] == policy) & (df["flows"] == f)]
            st = g[g["static_q"].notna()].sort_values("static_q")
            queues = [int(q) for q in st["static_q"]] or [1, 2048]
            if f == 10000 and (old := old_sweep(skew, policy)) is not None:
                ax.plot(old.index, old.values, color="#9a9a9a", lw=1, ls="--", label="sweep no-drop-zip")
            ax.plot(st["static_q"], st["ndr_mpps"], color=SWEEP, marker="o", ms=3, label="fixed queues")
            c = cmp[(cmp["policy"] == policy) & (cmp["flows"] == f)]
            if len(c) and "knee_q" in c and not np.isnan(c["knee_q"].iloc[0]):
                ax.axvline(c["knee_q"].iloc[0], color=SWEEP, lw=0.8, ls=":", label="knee")
            for name, sty in CONTROLLERS.items():
                r = g[g["mode"] == name]
                if len(r) and not np.isnan(r["queues"].iloc[0]):
                    ax.plot(r["queues"], r["ndr_mpps"], ls="", ms=7, mfc="none", mew=1.5, label=name, **sty)
            queue_axis(ax, queues)
            ax.set_title(f"{f} flows")
            ax.set_xlabel("RX queues")
        top = max(max(l.get_ydata(), default=0) for ax in axes[0] for l in ax.get_lines() if len(l.get_ydata()))
        axes[0][0].set_ylim(0, top * 1.08)
        axes[0][0].set_ylabel("zero-loss Mpps")
        handles, labels = axes[0][0].get_legend_handles_labels()
        for ax in axes[0][1:]:
            for h, l in zip(*ax.get_legend_handles_labels()):
                if l not in labels:
                    handles.append(h)
                    labels.append(l)
        fig.legend(handles, labels, loc="upper center", ncol=len(labels), bbox_to_anchor=(0.5, 1.08))
        fig.suptitle(f"{policy}, skew {skew:g}: controllers on the queue sweep", y=1.15, fontsize=9)
        fig.tight_layout()
        save(fig, out, f"sweep_{policy}")


def plot_summary(cmp, out):
    policies = [p for p in POLICIES if p in set(cmp["policy"])]
    fig, axes = plt.subplots(2, len(policies), figsize=(3.4 * len(policies), 5.0), squeeze=False, sharex=True)
    for col, policy in enumerate(policies):
        c = cmp[cmp["policy"] == policy].sort_values("flows")
        top, bottom = axes[0][col], axes[1][col]
        if "best_mpps" in c:
            top.plot(c["flows"], c["best_mpps"], color=SWEEP, marker="o", ms=3, label="best fixed")
            bottom.plot(c["flows"], c["knee_q"], color=SWEEP, marker="o", ms=3, ls=":", label="knee of the sweep")
        for name, sty in CONTROLLERS.items():
            if f"{name}_mpps" in c:
                top.plot(c["flows"], c[f"{name}_mpps"], ms=4, label=name, **sty)
                bottom.plot(c["flows"], c[f"{name}_q"], ms=4, label=name, **sty)
        top.set_title(policy)
        top.set_ylim(bottom=0)
        bottom.set_yscale("log", base=2)
        bottom.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        bottom.set_xscale("log")
        bottom.set_xlabel("active flows")
    axes[0][0].set_ylabel("zero-loss Mpps")
    axes[1][0].set_ylabel("queues used")
    axes[0][0].legend(fontsize=7)
    axes[1][0].legend(fontsize=7)
    fig.tight_layout()
    save(fig, out, "summary")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="?", default=os.path.join(HERE, "results", "adaptive", "ndr_v2"))
    parser.add_argument("--knee", type=float, default=2.0, help="percent below the best fixed rate")
    parser.add_argument("--out", help="default plots/adaptive/<name of the results folder>")
    args = parser.parse_args()
    out = args.out or os.path.join(HERE, "plots", "adaptive", os.path.basename(os.path.normpath(args.results)))
    os.makedirs(out, exist_ok=True)
    df = load(args.results)
    skew = float(pd.read_csv(os.path.join(args.results, "ndr.csv"))["skew"].iloc[0])
    cmp = comparison(df, args.knee)
    cmp.to_csv(os.path.join(args.results, "comparison.csv"), index=False)
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(cmp.round(3).to_string(index=False))
    plot_sweeps(df, cmp, skew, out)
    plot_summary(cmp, out)
    print(f"Plots in {out}/")


if __name__ == "__main__":
    main()
