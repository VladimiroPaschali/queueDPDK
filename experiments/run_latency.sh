#!/bin/bash
# Latency of the paper (latency vs queues at three offered loads, and the Pareto
# frontier of p99 latency against zero-loss throughput) for one app, results tagged
# -$TAG: every queue count, the 4 policies of latency.py (round robin, aggressive,
# latency, latency+aggressive), skew 0.6, from 1 Mpps to 0.5 Mpps above the highest
# zero-loss rate of the app's NDR run with the same tag (run_ndr.sh), in steps of 0.1.
#
#   ./run_latency.sh mica
#   ./run_latency.sh nf-chain
. "$(dirname "$0")/lib.sh"
app=$1
app_names "$app"
case $app in
mica)
	lat_app=mica-prefetch
	run make -C ../mica
	mica_cleanup ;;
nf-chain)
	lat_app=nf-chain
	run make -C ../nf-chain ;;
esac
ndr=results/ndr/results_${SINGLE}-${TAG}_skew0.6/benchmark_results.csv
if [ -n "$DRY_RUN" ]; then
	max='<highest zero-loss rate of '$ndr' + 0.5>'
else
	[ -f "$ndr" ] || { echo "no $ndr: run ./run_ndr.sh $app first" >&2; exit 1; }
	max=$(python3 -c "
import csv, math
best = max(float(r['tx_pps']) for r in csv.DictReader(open('$ndr')))
print(math.ceil(best / 1e6 * 2) / 2 + 0.5)")
fi
# The queue of the probe flow depends on the NIC's hash: find it again on this
# bitstream once (nf-chain prints the per-queue histogram MICA lacks); it is cached in
# latency_queues.json, which adaptive.py reads too
discover=results/latency/discover_$TAG
if [ ! -f "$discover/latency_results.csv" ]; then
	run python3 latency.py --app nf-chain --rediscover --policies baseline --min 1.0 --max 1.0 \
		--bitstream "$TAG" --output "$discover"
fi
run python3 latency.py --app "$lat_app" --bitstream "$TAG" --min 1.0 --max "$max" --step 0.1 --skew 0.6 \
	--resume --output "results/latency/results_latency_${lat_app}-${TAG}_skew0.6"
