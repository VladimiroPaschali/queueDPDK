#!/bin/bash
# Fig. 1c: average number of distinct flows in an RX burst of 32 packets against the
# number of RX queues (10k Zipf flows, skews 0.1, 0.6 and 0.9, 30 Mpps offered). The app
# of apps/locality records the 5-tuples of every burst, locality-bench.py analyses them.
# Results in results/locality/ (locality_summary.csv, one folder per skew and queues).
#
#   ./run_locality.sh
. "$(dirname "$0")/lib.sh"
run make -C apps/locality
run python3 apps/locality/locality-bench.py
