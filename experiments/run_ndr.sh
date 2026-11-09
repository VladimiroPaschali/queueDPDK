#!/bin/bash
# Zero-loss (RFC2544 NDR) runs of the paper for one app, with TRex on the other host,
# results tagged -$TAG (results/ndr/results_<experiment>-<TAG>_skew*):
#  - 1 core, every queue count (single-core, L1 misses and motivation figures): MICA with
#    baseline (round robin) and aggressive, nf-chain with baseline, penalty and
#    aggressive+penalty;
#  - Partitioned on 2 and 4 cores (multicore figure): MICA with aggressive, nf-chain with
#    aggressive+penalty only;
#  - the "theoretical max": the same Zipf flows sent one after the other
#    (sequential-profile.py) on 1 queue.
# Skews 0.6, 0.9 and 0.1, 1 repetition, skew by skew. The second pass repeats the
# searches that failed (--resume skips the points done).
#
#   ./run_ndr.sh mica
#   ./run_ndr.sh nf-chain
. "$(dirname "$0")/lib.sh"
app=$1
app_names "$app"
case $app in
mica)
	run make -C ../mica
	mica_cleanup
	POLICIES="baseline aggressive"
	PART_POLICY=aggressive ;;
nf-chain)
	run make -C ../nf-chain
	POLICIES="baseline penalty aggressive+penalty"
	PART_POLICY=aggressive+penalty ;;
esac
COMMON=(--tag="-$TAG" --repetitions 1 --resume)
for pass in 1 2; do
	for skew in 0.6 0.9 0.1; do
		run python3 no-drop-zip.py "$SINGLE" --queues $ALL_QUEUES --policies $POLICIES --skews $skew "${COMMON[@]}"
		run python3 no-drop-zip.py "${PART}2" "${PART}4" --policies $PART_POLICY --skews $skew "${COMMON[@]}"
	done
	# the bound: its own repetitions (nf-chain 3, MICA 1)
	run python3 no-drop-zip.py "$BOUND" --skews 0.1 0.6 0.9 --tag="-$TAG" --resume
done
