#!/bin/bash
# Theoretical max of mica-fifo-nocopy on one core with the 1024-descriptor ring,
# the sequential profile sent by two TRex cores (one tops out at ~7.4 Mpps),
# skews 0.1, 0.6 and 0.9, 1 repetition (see mica-fifo-nocopy-seq-trex2).
cd "$(dirname "$0")"
set -ex
make -C ../mica
python3 no-drop-zip.py mica-fifo-nocopy-seq-trex2 --resume
