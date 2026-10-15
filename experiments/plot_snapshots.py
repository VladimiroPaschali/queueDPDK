"""
Queue snapshots of the adaptive app (adaptive.py --snapshot-ms): how many
packets wait in every active RX queue at the start of a sweep, which queues the
next polls read and how many packets they take out.

  python3 plot_snapshots.py results/adaptive/timeline
  python3 plot_snapshots.py results/adaptive/timeline --scenario flows-rate --policy aggressive

One figure per scenario, policy and mode, one panel per phase (the snapshot
closest to the middle of the phase), into plots/adaptive/<results folder>/:
  snapshots_<scenario>_<policy>_<mode>
Grey bars are the packets waiting in each queue. The queues read by the next
48 polls that returned packets (empty polls are skipped and counted) are
coloured: the dark part is what the polls took out of the queue,
the light part what was left; the numbers over the first queues read give the
order of the polls (Aggressive reads a full queue several times in a row).
"""

import argparse
import os
from collections import OrderedDict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

import plot_adaptive as pa  # noqa: E402  (run names and colors)

HERE = os.path.dirname(os.path.abspath(__file__))
INK = "#0b0b0b"
INK_2 = "#52514e"
WAITING = "#b9b8b3"
FIRST_LABELS = 6


def parse_pairs(text):
    out = []
    for item in str(text).split(","):
        if ":" in item:
            a, b = item.split(":")
            out.append((int(a), int(b)))
    return out


def pick(snaps, phases):
    """The snapshot closest to the middle of every phase."""
    out = []
    for ph, g in snaps.groupby("phase"):
        mid = (g.t.min() + g.t.max()) / 2
        out.append(g.iloc[(g.t - mid).abs().argsort().iloc[0]])
    return out


def panel(ax, snap, color):
    queues = int(snap["queues"])
    waiting = dict(parse_pairs(snap["backlog"]))
    polls = parse_pairs(snap["polls"])
    taken = OrderedDict()
    for q, n in polls:
        taken[q] = taken.get(q, 0) + n
    xs = range(queues)
    width = 0.8 if queues <= 64 else 1.0
    ax.bar(xs, [waiting.get(q, 0) for q in xs], width=width, color=WAITING, lw=0)
    for q, n in taken.items():
        w = waiting.get(q, 0)
        ax.bar(q, min(n, w) if w else n, width=width, color=color, lw=0)
        if w > n:
            ax.bar(q, w - n, bottom=n, width=width, color=color, alpha=0.35, lw=0)
    # order of the first polls (a queue read several times in a row counts once)
    order, last = [], None
    for q, _ in polls:
        if q != last:
            order.append(q)
            last = q
    top = max([waiting.get(q, 0) for q in xs] + [1])
    for k, q in enumerate(order[:FIRST_LABELS], start=1):
        ax.annotate(str(k), (q, max(waiting.get(q, 0), taken.get(q, 0))), xytext=(0, 2), textcoords="offset points",
                    ha="center", fontsize=6, color=INK)
    first = polls[0][0] if polls else None
    runs = [n for q, n in polls if q == first]
    seq = " ".join(f"{q}:{n}" for q, n in polls[:8])
    ax.set_title(f"{snap['offered_mpps']} Mpps, {int(snap['flows'])} flows, {queues} queues, t={snap['t']:.0f} s",
                 fontsize=7.5)
    empty = int(snap.get("empty_polls", 0) or 0)
    ax.text(0.99, 0.97, f"next polls with packets (queue:packets), {empty} empty polls in between\n{seq}",
            transform=ax.transAxes, ha="right", va="top",
            fontsize=6, color=INK_2)
    ax.set_xlim(-1, queues)
    ax.set_ylim(0, top * 1.25)
    ax.set_ylabel("packets waiting", fontsize=7)
    ax.tick_params(labelsize=6.5)
    return runs


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="?", default=os.path.join(HERE, "results", "adaptive", "timeline"))
    parser.add_argument("--scenario", nargs="*")
    parser.add_argument("--policy", nargs="*")
    parser.add_argument("--out")
    args = parser.parse_args()
    out = args.out or os.path.join(HERE, "plots", "adaptive", os.path.basename(os.path.normpath(args.results)))
    os.makedirs(out, exist_ok=True)
    snaps = pd.read_csv(os.path.join(args.results, "snapshots.csv"), keep_default_na=False)
    snaps = snaps.assign(name=snaps.apply(pa.run_name, axis=1))
    for (scenario, policy, name), g in snaps.groupby(["scenario", "policy", "name"]):
        if (args.scenario and scenario not in args.scenario) or (args.policy and policy not in args.policy):
            continue
        chosen = pick(g, None)
        color = (pa.ADAPTIVE_COLOR if pa.is_adaptive(name)
                 else pa.STATIC_COLORS[sorted(snaps.name.unique()).index(name) % len(pa.STATIC_COLORS)])
        fig, axes = plt.subplots(len(chosen), 1, figsize=(10, 1.55 * len(chosen) + 0.5), squeeze=False)
        for ax, snap in zip(axes[:, 0], chosen):
            panel(ax, snap, color)
        axes[-1][0].set_xlabel("RX queue")
        fig.suptitle(f"{scenario}, {policy}, {name}: packets waiting per queue (grey), taken by the next polls "
                     f"(dark) and left (light)", fontsize=9, color=INK)
        fig.tight_layout(rect=(0, 0, 1, 0.98))
        tag = name.replace(" ", "")
        for ext in ("pdf", "png"):
            fig.savefig(os.path.join(out, f"snapshots_{scenario}_{policy}_{tag}.{ext}"), dpi=150,
                        bbox_inches="tight")
        plt.close(fig)
        print(f"  snapshots_{scenario}_{policy}_{tag}.pdf/.png")
    print(f"Plots in {out}/")


if __name__ == "__main__":
    main()
