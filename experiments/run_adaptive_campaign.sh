#!/bin/bash
# Queue of 14 Oct: new timelines (signal against fixed queue counts, queue snapshots),
# then the rest of the NDR campaign. Everything already done is skipped.
# The signal thresholds are read from adaptive_signal_args.txt.
cd "$(dirname "$0")"
SIG() { cat adaptive_signal_args.txt 2>/dev/null; }
set -x
# Timelines: throughput (load) and flows (flows-rate, 2.5 Mpps) changing over time; flow starts
# spread over 20 ms (no initial burst); snapshots of the queues every 4 s
python3 adaptive.py --output results/adaptive/timeline --scenarios load flows-rate --policies baseline aggressive penalty --variants "$(SIG)" --static 1 128 512 2048 --phase-duration 20 --start-spread-us 20000 --snapshot-ms 4000
python3 plot_adaptive.py results/adaptive/timeline
python3 plot_snapshots.py results/adaptive/timeline
[ -f plots/adaptive/timeline/timeline_load_aggressive.png ] && rm -rf results/adaptive/timeline_old plots/adaptive/timeline_old
# Adaptive timeline with the flows changing at 3 Mpps (signal against fixed queue counts, snapshots)
python3 adaptive.py --output results/adaptive/timeline --scenarios flows-rate3 --policies baseline aggressive penalty --variants "$(SIG)" --static 1 128 512 2048 --phase-duration 20 --start-spread-us 20000 --snapshot-ms 4000
python3 plot_adaptive.py results/adaptive/timeline --scenario flows-rate3
python3 plot_snapshots.py results/adaptive/timeline --scenario flows-rate3
# Lowest priority: the rest of the NDR campaign stopped on 12 Oct (normal search)
python3 no-drop-zip.py mica-partitioned2 --resume
python3 no-drop-zip.py nf-chain-partitioned4 mica-partitioned4 nf-chain-shared8 mica-shared8 nf-chain-partitioned8 mica-partitioned8 --resume
