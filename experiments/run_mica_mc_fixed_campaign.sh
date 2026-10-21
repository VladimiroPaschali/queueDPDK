#!/bin/bash
# Gate of the MICA multicore campaign: waits for logs/mica_mc_gate, created
# when the single-core prefetch tests that follow the starvation campaign are
# done, then runs run_mica_mc_fixed_body.sh (read only then, so it can still be
# changed while this waits).
cd "$(dirname "$0")"
echo "waiting for logs/mica_mc_gate"
while [ ! -f logs/mica_mc_gate ]; do sleep 30; done
exec ./run_mica_mc_fixed_body.sh
