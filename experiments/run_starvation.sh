#!/bin/bash
# Starvation of the paper: p99 flow completion time (FCT) of a 1024-packet flow sent at
# 200 kpps on top of 2 Mpps of low-skew traffic (skew 0.6), round robin and aggressive,
# 1 to 2048 queues. The app of nf-chain is apps/starvation (nf-chain plus the poll log
# of common/qprobe.h), MICA's is mica-fifo-nocopy-prefetch-qprobe. Results tagged -$TAG
# in results/starvation/<app>-<TAG>.
#
#   ./run_starvation.sh mica
#   ./run_starvation.sh nf-chain
. "$(dirname "$0")/lib.sh"
app=$1
case $app in
mica)
	run make -C ../mica
	mica_cleanup
	starv_app=mica-prefetch ;;
nf-chain)
	run make -C apps/starvation
	starv_app=nf-chain ;;
*)
	echo "unknown app $app (mica or nf-chain)" >&2
	exit 1 ;;
esac
run python3 starvation.py --app "$starv_app" --skews 0.6 --rates 2.0 --policies baseline aggressive \
	--output "results/starvation/$app-$TAG"
