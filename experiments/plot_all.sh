#!/bin/bash
# Every plot the paper's figures come from, from the results of a campaign tagged $TAG
# (run_all.sh), into plots/:
#   plots/ndr/<TAG>/             throughput and per-packet events against the queues: 1 core
#                                (motivation, single-core figures) and Partitioned on 1/2/4 cores
#   plots/bound/<TAG>/           policies against the "theoretical max" and round robin on 1 queue
#   plots/latency/<app>-<TAG>/   p99 latency against the queues at three loads, Pareto frontier
#   plots/starvation/<app>-<TAG>/ flow completion time against the queues
#   plots/adaptive/<app>-<TAG>/  timelines of the adaptive controller
# A plot whose results are missing is skipped. The data of the LaTeX figures are the
# CSVs behind these plots. Fig. 1c (distinct flows per burst) is
# results/locality/locality_summary.csv, not plotted here.
#
#   ./plot_all.sh                 # TAG=pfch64
#   TAG=other ./plot_all.sh
#   UNTAGGED_BOUND=1 ./plot_all.sh   # bound and round robin lines from the untagged runs
#                                    # (results of before the tagged campaign)
. "$(dirname "$0")/lib.sh"
set +e
failed=0

# plot <results dir that must exist> <command...>
plot() {
	local dir=$1
	shift
	if [ ! -d "$dir" ] && [ -z "$DRY_RUN" ]; then
		echo "skipped, no $dir: $*"
		return
	fi
	run "$@" || { echo "FAILED: $*"; failed=1; }
}

# --- zero-loss throughput. plot_ndr.py reads an app's experiments as <app>,
# <app>-partitioned<c>, so the tagged folders are linked under those names in a
# temporary directory (MICA's experiments have longer names)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
link() { # <name for plot_ndr.py> <experiment>
	for d in results/ndr/results_$2-${TAG}_skew*; do
		[ -f "$d/benchmark_results.csv" ] && ln -s "$PWD/$d" "$tmp/results_$1_skew${d##*_skew}"
	done
}
link mica mica-fifo-nocopy-prefetch
link nf-chain nf-chain
for c in 2 4; do
	link mica-partitioned$c mica-fifo-nocopy-pools-prefetch-partitioned$c
	link nf-chain-partitioned$c nf-chain-partitioned$c
done
plot results/ndr python3 plot_ndr.py --results "$tmp" --out "plots/ndr/$TAG" --apps mica nf-chain \
	mica-partitioned2 mica-partitioned4 nf-chain-partitioned2 nf-chain-partitioned4

# --- policies against the theoretical max
lines=--lines-tagged
[ -z "$UNTAGGED_BOUND" ] || lines=
plot results/ndr python3 plot_bound.py --tag "$TAG" $lines --apps nf-chain
plot results/ndr python3 plot_bound.py --tag "$TAG" $lines --apps mica-fifo-nocopy-prefetch --bound seq-trex2

# --- latency (the offered loads of the figure panels)
plot "results/latency/results_latency_nf-chain-${TAG}_skew0.6" python3 plot_latency.py \
	"results/latency/results_latency_nf-chain-${TAG}_skew0.6" --out "plots/latency/nf-chain-$TAG" \
	--rate 2.0 --panel-rates 2.0 2.8 3.6
plot "results/latency/results_latency_mica-prefetch-${TAG}_skew0.6" python3 plot_latency.py \
	"results/latency/results_latency_mica-prefetch-${TAG}_skew0.6" --out "plots/latency/mica-prefetch-$TAG" \
	--rate 4.0 --panel-rates 1.8 4.6 6.2

# --- starvation, adaptive
for app in nf-chain mica; do
	plot "results/starvation/$app-$TAG" python3 plot_starvation.py "results/starvation/$app-$TAG" \
		--out "plots/starvation/$app-$TAG"
	plot "results/adaptive/$app-$TAG" python3 plot_adaptive.py "results/adaptive/$app-$TAG" \
		--out "plots/adaptive/$app-$TAG"
done
exit $failed
