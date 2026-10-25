"""
Plots for the adaptive queue-count runs of adaptive.py.

  python3 plot_adaptive.py                      # results/adaptive/runs/
  python3 plot_adaptive.py results/adaptive/timeline --scenario load

Writes PDF and PNG into plots/adaptive/<name of the results folder>/ (--out):
  timeline_<scenario>_<policy>   over time: offered and returned Mpps (adaptive
                                 and every fixed queue count), the active queues
                                 chosen by the controller with its changes, the
                                 p99 latency of the TRex probes and of the
                                 latency flow in every second (runs with
                                 probes; dashed where the loss of that second
                                 is above --loss-threshold) and the loss; phase
                                 changes are marked
  phases_<scenario>_<policy>     steady-state Mpps per phase, adaptive against
                                 the fixed queue counts, with the queue count
                                 the controller settled on
  latency_<scenario>_<policy>    probe latency per phase (p99 bars, p50 ticks),
                                 adaptive against the fixed queue counts, over
                                 the whole phase (transient included) and over
                                 its second half; hatched where the loss is
                                 above --loss-threshold
  latencyflow_<scenario>_<policy> the same for the latency flow, the one the
                                 +latency policies poll every few polls
"""

import argparse
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LOSS_THRESHOLD = 0.1  # % (pdr of the NDR searches): latency above it is drawn dashed or hatched
LATENCY = {"lat": "probes", "lq": "latency flow"}
ADAPTIVE_COLORS = ["#eb6834", "#c4207a", "#8c5a00"]
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


def sort_names(names):
    return sorted(names, key=lambda n: (not is_adaptive(n), len(n), n))


def color_map(names):
    """Same color for a run in every plot: adaptive runs warm, fixed ones cool."""
    adaptive = [n for n in sort_names(names) if is_adaptive(n)]
    fixed = [n for n in sort_names(names) if not is_adaptive(n)]
    return ({n: ADAPTIVE_COLORS[i % len(ADAPTIVE_COLORS)] for i, n in enumerate(adaptive)}
            | {n: STATIC_COLORS[i % len(STATIC_COLORS)] for i, n in enumerate(fixed)})


def phase_label(ph, prev):
    """Only what changed from the previous phase."""
    parts = []
    for k, fmt in (("offered_mpps", "{} Mpps"), ("skew", "s={}"), ("flows", "{} fl"), ("size", "{}B")):
        if prev is None or ph[k] != prev[k]:
            parts.append(fmt.format(ph[k]))
    return "\n".join(parts)


def phase_ticks(first, phases):
    """Tick labels with only what changes across the phases of the scenario."""
    cols = dict(offered_mpps="rate", skew="skew", flows="flows", size="size")
    rows = [dict((k, first.loc[p][c]) for k, c in cols.items()) for p in phases]
    varying = [k for k in cols if len({str(r[k]) for r in rows}) > 1] or ["offered_mpps"]
    fmt = dict(offered_mpps="{} Mpps", skew="s={}", flows="{} fl", size="{}B")
    return ["\n".join(fmt[k].format(r[k]) for k in varying) for r in rows]


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
    names = sort_names(tl.name.unique())
    adaptive = [n for n in names if is_adaptive(n)]
    colors = color_map(names)

    kinds = [k for k in LATENCY if has_values(tl, f"{k}_p99")]
    fig, axes = plt.subplots(3 + len(kinds), 1, figsize=(10, 7.5 + 2 * len(kinds)), sharex=True,
                             gridspec_kw=dict(height_ratios=[3, 2] + [2] * len(kinds) + [1.2]))
    ax_rx, ax_q, ax_loss = axes[0], axes[1], axes[-1]
    ax_lat = dict(zip(kinds, axes[2:-1]))
    ref = tl[tl.name == names[0]]
    ax_rx.step(ref.t, ref.offered_mpps, where="post", color="black", lw=1, ls=":", label="offered")
    for n in names:
        d = tl[tl.name == n]
        lw = 2.0 if n in adaptive else 1.0
        ax_rx.plot(d.t, d.rx_pps / 1e6, color=colors[n], lw=lw, label=n)
        loss = pd.to_numeric(d.loss_pct, errors="coerce")
        ax_loss.plot(d.t, loss, color=colors[n], lw=lw)
        for k, ax in ax_lat.items():
            # the whole line dashed, then solid over the seconds within the loss threshold
            y = pd.to_numeric(d[f"{k}_p99"], errors="coerce")
            ax.plot(d.t, y, color=colors[n], lw=lw, ls=(0, (3, 2)))
            ax.plot(d.t, y.where(loss.fillna(0) <= LOSS_THRESHOLD), color=colors[n], lw=lw)
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
        for ax in axes:
            ax.axvline(t0 - 0.5, color="grey", lw=0.6, ls="--")
        ax_rx.annotate(phase_label(ph, prev), (t0 - 0.3, 1.0), xycoords=("data", "axes fraction"),
                       va="top", fontsize=7, color="dimgrey")
        prev = ph

    ax_rx.set_ylabel("Mpps")
    ax_rx.legend(fontsize=7, ncol=3, loc="lower right")
    ax_q.set_yscale("log", base=2)
    ax_q.set_ylabel("active queues")
    ax_q.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:g}"))
    for k, ax in ax_lat.items():
        ax.set_yscale("log")
        ax.set_ylabel(f"{LATENCY[k]} p99 (µs)")
    if ax_lat:
        dashed = plt.Line2D([], [], color="dimgrey", lw=1, ls=(0, (3, 2)), label=f"loss > {LOSS_THRESHOLD:g}%")
        next(iter(ax_lat.values())).legend(handles=[dashed], fontsize=7, loc="upper left")
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
    names = sort_names(agg.name.unique())
    colors = color_map(names)
    phases = sorted(agg.phase.unique())
    first = ph.drop_duplicates("phase").set_index("phase")
    width = 0.8 / len(names)

    fig, ax = plt.subplots(figsize=(max(6, 1.3 * len(phases)), 3.8))
    for i, n in enumerate(names):
        d = agg[agg.name == n].set_index("phase").reindex(phases)
        x = np.arange(len(phases)) + (i - (len(names) - 1) / 2) * width
        bars = ax.bar(x, d.rx / 1e6, width, label=n, color=colors[n],
                      alpha=1.0 if is_adaptive(n) else 0.8)
        if is_adaptive(n):
            for b, q in zip(bars, d.q):
                if pd.notna(q) and q != "":
                    ax.annotate(f"{int(float(q))}q", (b.get_x() + b.get_width() / 2, b.get_height()),
                                ha="center", va="bottom", fontsize=6)
    labels = phase_ticks(first, phases)
    ax.set_xticks(np.arange(len(phases)))
    ax.set_xticklabels(labels, fontsize=7)
    ax.set_ylabel("steady Mpps")
    ax.legend(fontsize=7, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    ax.set_title(f"{scenario}, {policy}")
    save(fig, out, f"phases_{scenario}_{policy}")


def has_values(df, col):
    return col in df and pd.to_numeric(df[col], errors="coerce").notna().any()


def plot_latency(ph, scenario, policy, out, kind="lat"):
    """p99 (bars) and p50 (ticks) per phase of the probes (kind lat) or of the
    latency flow (lq), whole phase and steady half; hatched above the loss
    threshold."""
    ph = ph[(ph.scenario == scenario) & (ph.policy == policy)]
    if ph.empty or not has_values(ph, f"{kind}_p99"):
        return
    ph = ph.assign(name=ph.apply(run_name, axis=1))
    names = sort_names(ph.name.unique())
    colors = color_map(names)
    phases = sorted(ph.phase.unique())
    first = ph.drop_duplicates("phase").set_index("phase")
    width = 0.8 / len(names)

    fig, axes = plt.subplots(2, 1, figsize=(max(6, 1.3 * len(phases)), 6.5), sharex=True)
    for ax, prefix, loss_col, title in ((axes[0], f"{kind}_", "loss_pct", "whole phase"),
                                        (axes[1], f"steady_{kind}_", "steady_loss_pct", "second half")):
        for i, n in enumerate(names):
            d = ph[ph.name == n].groupby("phase")[[f"{prefix}p99", f"{prefix}p50", loss_col]].agg(
                lambda v: pd.to_numeric(v, errors="coerce").mean()).reindex(phases)
            x = np.arange(len(phases)) + (i - (len(names) - 1) / 2) * width
            bars = ax.bar(x, d[f"{prefix}p99"], width, label=n, color=colors[n], alpha=1.0 if is_adaptive(n) else 0.8)
            for b, loss in zip(bars, d[loss_col]):
                if pd.notna(loss) and loss > LOSS_THRESHOLD:
                    b.set_hatch("///")
                    b.set_edgecolor("white")
            ax.scatter(x, d[f"{prefix}p50"], marker="_", s=60, color="black", zorder=3,
                       label="p50" if i == 0 else None)
        ax.set_yscale("log")
        ax.set_ylabel(f"{LATENCY[kind]} p99 (µs)")
        ax.set_title(title, fontsize=9)
    axes[1].set_xticks(np.arange(len(phases)))
    axes[1].set_xticklabels(phase_ticks(first, phases), fontsize=7)
    handles, labels = axes[0].get_legend_handles_labels()
    handles.append(Patch(facecolor="lightgrey", edgecolor="white", hatch="///"))
    labels.append(f"loss > {LOSS_THRESHOLD:g}%")
    axes[0].legend(handles, labels, fontsize=7, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.suptitle(f"{scenario}, {policy}: latency of the {LATENCY[kind]}")
    save(fig, out, f"{'latency' if kind == 'lat' else 'latencyflow'}_{scenario}_{policy}")


def main():
    global LOSS_THRESHOLD
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="?", default=os.path.join(HERE, "results", "adaptive", "runs"))
    parser.add_argument("--scenario", nargs="*", help="only these scenarios")
    parser.add_argument("--out", help="default plots/adaptive/<name of the results folder>")
    parser.add_argument("--loss-threshold", type=float, default=LOSS_THRESHOLD,
                        help="loss %% above which latency is drawn dashed (timeline) or hatched (bars)")
    args = parser.parse_args()
    LOSS_THRESHOLD = args.loss_threshold

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
            for kind in LATENCY:
                plot_latency(ph, scenario, policy, out, kind)
    print(f"plots in {out}/")


if __name__ == "__main__":
    main()
