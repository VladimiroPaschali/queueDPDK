#!/bin/bash
# Latency (Fig. 6: Pareto, latency_vs_queues_grid_p99) and starvation/FCT of
# the corrected single-core MICA, as the nf-chain campaigns of 5-6 Oct.
# latency.py: every queue count, the 4 policies, skew 0.6, 1.0-7.0 Mpps in
# steps of 0.1 (nf-chain went to 5.0; MICA forwards up to ~6.2 Mpps on a core).
# starvation.py: queues 1 64 128 1024 2048, the 4 policies, skews 0.1/0.6/0.9,
# 2/4/6 Mpps (nf-chain's 1/2/3 scaled to MICA's ~2x per-core rate).
# Results in results/latency/results_latency_mica_skew0.6 and
# results/starvation/mica, plots in plots/latency/mica and plots/starvation/mica.
cd "$(dirname "$0")"
set -ex
make -C ../mica
python3 latency.py --app mica --min 1.0 --max 7.0 --step 0.1 --skew 0.6 --resume
python3 plot_latency.py results/latency/results_latency_mica_skew0.6 --out plots/latency/mica --rate 4.0 \
  --panel-rates 1.0 1.4 1.8 2.2 2.6 3.0 3.4 3.8 4.2 4.6 5.0 5.4 5.8 6.2 6.6 7.0 || true
python3 starvation.py --app mica --rates 2.0 4.0 6.0 --output results/starvation/mica
python3 plot_starvation.py results/starvation/mica --out plots/starvation/mica --rate 4.0 || true
