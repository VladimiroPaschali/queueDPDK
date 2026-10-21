#!/bin/bash
# After run_mica_mc_fixed_body.sh, on the same fixed MICA (16-byte key, one log
# pool per lcore, MICA's request pipeline):
# - the paper's Shared, every lcore polling every queue (padded queue locks),
#   2 and 4 cores, baseline/penalty/aggressive+penalty, skews 0.6/0.9/0.1,
#   every queue count (the second pass repeats failed searches);
# - latency (Fig. 6, Pareto, latency_vs_queues_grid_p99) and starvation/FCT of
#   the single-core MICA, as run_mica_lat_starv.sh with --app mica-prefetch.
# Results in results/ndr/results_mica-fifo-nocopy-pools-padlock-prefetch-shared{2,4}_skew*,
# results/latency/results_latency_mica-prefetch_skew0.6, results/starvation/mica-prefetch.
cd "$(dirname "$0")"
set -ex
make -C ../mica
POLICIES="baseline penalty aggressive+penalty"
for pass in 1 2; do
  for cores in 2 4; do
    for skew in 0.6 0.9 0.1; do
      python3 no-drop-zip.py mica-fifo-nocopy-pools-padlock-prefetch-shared$cores \
        --policies $POLICIES --skews $skew --repetitions 1 --resume
    done
  done
done
python3 latency.py --app mica-prefetch --min 1.0 --max 7.0 --step 0.1 --skew 0.6 --resume
python3 plot_latency.py results/latency/results_latency_mica-prefetch_skew0.6 --out plots/latency/mica-prefetch \
  --rate 4.0 --panel-rates 1.0 1.4 1.8 2.2 2.6 3.0 3.4 3.8 4.2 4.6 5.0 5.4 5.8 6.2 6.6 7.0 || true
python3 starvation.py --app mica-prefetch --rates 2.0 4.0 6.0 --output results/starvation/mica-prefetch
python3 plot_starvation.py results/starvation/mica-prefetch --out plots/starvation/mica-prefetch --rate 4.0 || true
