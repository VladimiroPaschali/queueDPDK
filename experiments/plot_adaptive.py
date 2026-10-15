"""
Plots for the adaptive queue-count runs of adaptive.py.

  python3 plot_adaptive.py                      # results/adaptive/runs/
  python3 plot_adaptive.py results/adaptive/timeline --scenario load

Writes PDF and PNG into plots/adaptive/<name of the results folder>/ (--out):
  timeline_<scenario>_<policy>   over time: offered and returned Mpps (adaptive
                                 and every fixed queue count), the active queues
                                 chosen by the controller with its changes, and
                                 the loss; phase changes are marked
  phases_<scenario>_<policy>     steady-state Mpps per phase, adaptive against
                                 the fixed queue counts, with the queue count
                                 the controller settled on
"""

import argparse
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTIVE_COLOR = "#eb6834"
STATIC_COLORS = ["#2a78d6", "#1baf7a", "#7a5fc9", "#8a8a8a", "#c9a227"]


def run_name(r):
    """fixed N or signal (the controller of the adaptive app), plus any extra
    option of the run (runs before the scan controller was removed carry
    --adaptive-mode signal)."""
    if r["mode"] != "adaptive":
        return r["mode"].replace("static-", "fixed ")
    rest = str(r["variant"]).replace("--adaptive-mode signal", "").strip()
    return "signal" + (f" {rest}" if rest else "")


def is_adaptive(name):
    return not name.startswith("fixed")


def phase_label(ph, prev):
    """Only what changed from the previous phase."""
    parts = []
    for k, fmt in (("offered_mpps", "{} Mpps"), ("skew", "s={}"), ("flows", "{} fl"), ("size", "{}B")):
        if prev is None or ph[k] != prev[k]:
            parts.append(fmt.format(ph[k]))
    return "\n".join(parts)


def save(fig, out, name):
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out, f"{name}.{ext}"), bbox_inches="tight", dpi=150)
    plt.close(fig)


def plot_timeline(tl, dec, scenario, policy, out):
    tl = tl[(tl.scenario == scenario) & (tl.policy == policy) & (tl.rep == tl.rep.min())]
    dec = dec[(dec.scenario == scenario) & (dec.policy == policy) & (dec.rep == tl.rep.min())] \
        if dec is not None else None
    if tl.empty:
        return
    tl = tl.assign(name=tl.apply(run_name, axis=1))
    names = sorted(tl.name.unique(), key=lambda n: (not is_adaptive(n), len(n), n))
    adaptive = [n for n in names if is_adaptive(n)]
    colors = {n: ADAPTIVE_COLOR for n in adaptive}
    colors |= {n: STATIC_COLORS[i % len(STATIC_COLORS)] for i, n in enumerate(n for n in names if n not in adaptive)}

    fig, (ax_rx, ax_q, ax_loss) = plt.subplots(3, 1, figsize=(10, 7.5), sharex=True,
                                              gridspec_kw=dict(height_ratios=[3, 2, 1.2]))
    ref = tl[tl.name == names[0]]
    ax_rx.step(ref.t, ref.offered_mpps, where="post", color="black", lw=1, ls=":", label="offered")
    for n in names:
        d = tl[tl.name == n]
        lw = 2.0 if n in adaptive else 1.0
        ax_rx.plot(d.t, d.rx_pps / 1e6, color=colors[n], lw=lw, label=n)
        ax_loss.plot(d.t, pd.to_numeric(d.loss_pct, errors="coerce"), color=colors[n], lw=lw)
        if n not in adaptive:
            continue
        q = pd.to_numeric(d.queues, errors="coerce")
        ax_q.step(d.t, q, where="post", color=colors[n], lw=lw, label=n)
        if dec is not None:
            dd = dec[dec.apply(run_name, axis=1) == n]
            ax_q.scatter(dd.t, dd.to_q, s=8, color=colors[n], alpha=0.5, zorder=3)

    # phase boundaries
    starts = ref.groupby("phase").t.min()
    prev = None
    for p, t0 in starts.items():
        ph = ref[ref.phase == p].iloc[0]
        for ax in (ax_rx, ax_q, ax_loss):
            ax.axvline(t0 - 0.5, color="grey", lw=0.6, ls="--")
        ax_rx.annotate(phase_label(ph, prev), (t0 - 0.3, 1.0), xycoords=("data", "axes fraction"),
                       va="top", fontsize=7, color="dimgrey")
        prev = ph

    ax_rx.set_ylabel("Mpps")
    ax_rx.legend(fontsize=7, ncol=3, loc="lower right")
    ax_q.set_yscale("log", base=2)
    ax_q.set_ylabel("active queues")
    ax_q.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:g}"))
    ax_loss.set_ylabel("loss %")
    ax_loss.set_xlabel("time (s)")
    fig.suptitle(f"{scenario}, {policy}")
    save(fig, out, f"timeline_{scenario}_{policy}")


def plot_phases(ph, scenario, policy, out):
    ph = ph[(ph.scenario == scenario) & (ph.policy == policy)]
    if ph.empty:
        return
    ph = ph.assign(name=ph.apply(run_name, axis=1))
    agg = ph.groupby(["phase", "name"]).agg(rx=("steady_rx_pps", "mean"), q=("settled_queues", "first")).reset_index()
    names = sorted(agg.name.unique(), key=lambda n: (not is_adaptive(n), len(n), n))
    phases = sorted(agg.phase.unique())
    first = ph.drop_duplicates("phase").set_index("phase")
    width = 0.8 / len(names)

    fig, ax = plt.subplots(figsize=(max(6, 1.3 * len(phases)), 3.8))
    for i, n in enumerate(names):
        d = agg[agg.name == n].set_index("phase").reindex(phases)
        x = np.arange(len(phases)) + (i - (len(names) - 1) / 2) * width
        color = (ADAPTIVE_COLOR if is_adaptive(n)
                 else STATIC_COLORS[i % len(STATIC_COLORS)])
        bars = ax.bar(x, d.rx / 1e6, width, label=n, color=color,
                      alpha=1.0 if is_adaptive(n) else 0.8)
        if is_adaptive(n):
            for b, q in zip(bars, d.q):
                if pd.notna(q) and q != "":
                    ax.annotate(f"{int(float(q))}q", (b.get_x() + b.get_width() / 2, b.get_height()),
                                ha="center", va="bottom", fontsize=6)
    labels = []
    for p in phases:
        r = first.loc[p]
        cur = dict(offered_mpps=r["rate"], skew=r["skew"], flows=r["flows"], size=r["size"])
        labels.append(phase_label(cur, None).replace("\n", ", ").replace(", s=", "\ns=", 1))
    ax.set_xticks(np.arange(len(phases)))
    ax.set_xticklabels(labels, fontsize=7)
    ax.set_ylabel("steady Mpps")
    ax.legend(fontsize=7, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    ax.set_title(f"{scenario}, {policy}")
    save(fig, out, f"phases_{scenario}_{policy}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="?", default=os.path.join(HERE, "results", "adaptive", "runs"))
    parser.add_argument("--scenario", nargs="*", help="only these scenarios")
    parser.add_argument("--out", help="default plots/adaptive/<name of the results folder>")
    args = parser.parse_args()

    tl = pd.read_csv(os.path.join(args.results, "timeline.csv"), keep_default_na=False)
    tl = tl.assign(variant=tl.variant.astype(str))
    ph = pd.read_csv(os.path.join(args.results, "phases.csv"), keep_default_na=False)
    ph = ph.assign(variant=ph.variant.astype(str), steady_rx_pps=pd.to_numeric(ph.steady_rx_pps, errors="coerce"))
    dec_path = os.path.join(args.results, "decisions.csv")
    dec = pd.read_csv(dec_path, keep_default_na=False) if os.path.exists(dec_path) else None
    if dec is not None:
        dec = dec.assign(variant=dec.variant.astype(str))
    for col in ("t", "rx_pps", "offered_mpps"):
        tl[col] = pd.to_numeric(tl[col], errors="coerce")

    out = args.out or os.path.join(HERE, "plots", "adaptive", os.path.basename(os.path.normpath(args.results)))
    os.makedirs(out, exist_ok=True)
    for scenario in args.scenario or tl.scenario.unique():
        for policy in tl[tl.scenario == scenario].policy.unique():
            plot_timeline(tl, dec, scenario, policy, out)
            plot_phases(ph, scenario, policy, out)
    print(f"plots in {out}/")


if __name__ == "__main__":
    main()
