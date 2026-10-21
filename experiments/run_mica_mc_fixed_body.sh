#!/bin/bash
# The paper's MICA multicore experiments (Fig. 9 and 10) on the fixed MICA:
# mica-fifo-nocopy with the 16-byte key fix, one log pool per lcore and MICA's
# request pipeline (prefetch), Partitioned (-pools-prefetch) and Shared with
# queue affinity (-pools-affinity-prefetch), 2 and 4 cores, baseline, penalty
# and aggressive+penalty, skews 0.1, 0.6 and 0.9, every queue count, 1
# repetition. Skew by skew both variants, so each pair is comparable as soon
# as it lands. Then the single-core curves of the same MICA (Fig. 8 and the
# 1-core line of Fig. 9) and its bound (sequential traffic, 2 TRex cores).
# The second pass repeats the NDR searches that failed; --resume skips what
# is done.
# Results in results/ndr/results_mica-fifo-nocopy-pools{,-affinity}-prefetch-{partitioned,shared}{2,4}_skew*,
# results_mica-fifo-nocopy-prefetch_skew* and results_mica-fifo-nocopy-prefetch-seq-trex2_skew*.
cd "$(dirname "$0")"
set -ex
make -C ../mica
POLICIES="baseline penalty aggressive+penalty"
for pass in 1 2; do
  for cores in 2 4; do
    for skew in 0.6 0.9 0.1; do
      python3 no-drop-zip.py mica-fifo-nocopy-pools-prefetch-partitioned$cores \
        mica-fifo-nocopy-pools-affinity-prefetch-shared$cores \
        --policies $POLICIES --skews $skew --repetitions 1 --resume
    done
  done
done
for pass in 1 2; do
  python3 no-drop-zip.py mica-fifo-nocopy-prefetch --queues 1 2 4 8 16 32 64 128 256 512 1024 2048 \
    --policies $POLICIES --skews 0.6 0.9 0.1 --repetitions 1 --resume
done
python3 no-drop-zip.py mica-fifo-nocopy-prefetch-seq-trex2 --skews 0.1 0.6 0.9 --resume
