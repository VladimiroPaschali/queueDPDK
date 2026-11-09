#!/bin/bash
# Adaptive queue count of the paper, one app, single core with 2048 configured queues:
# aggressive+latency (the policy polls the latency flow's queue every --latency-period
# polls) with the controller against fixed queue counts. TRex changes rate or number of
# flows phase by phase (adaptive.py SCENARIOS). Results in results/adaptive/<app>-<TAG>.
#
#   ./run_adaptive.sh nf-chain    # scenarios flows-rate35 (3.5 Mpps, changing flows) and ramp;
#                                 # the controller with its original thresholds and with
#                                 # --adaptive-search, against 1, 16, 128 and 2048 queues
#   ./run_adaptive.sh mica        # mica-flows-rate8 (8 Mpps, changing flows) and mica-ramp-max9
#                                 # (3 to 9 Mpps); --adaptive-search against 1 and 128 queues
. "$(dirname "$0")/lib.sh"
app=$1
case $app in
nf-chain)
	run make -C apps/adaptive
	SCENARIOS="flows-rate35 ramp"
	VARIANTS=('' ' --adaptive-search')
	STATIC="1 16 128 2048" ;;
mica)
	# mica-fifo-nocopy-prefetch-adaptive of mica/Makefile
	run make -C ../mica
	mica_cleanup
	SCENARIOS="mica-flows-rate8 mica-ramp-max9"
	VARIANTS=(' --adaptive-search')
	STATIC="1 128" ;;
*)
	echo "unknown app $app (mica or nf-chain)" >&2
	exit 1 ;;
esac
OUT=results/adaptive/$app-$TAG
for scenario in $SCENARIOS; do
	run python3 adaptive.py --app "$app" --scenarios "$scenario" --policies aggressive+latency \
		--variants "${VARIANTS[@]}" --static $STATIC --output "$OUT"
done
