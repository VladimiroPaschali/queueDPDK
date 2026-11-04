#!/bin/bash
# Every test of the paper, on the bitstream of ../extended_table_prefetch.bit (the one
# that prints "bitstream: pfch64", the tag of the results):
#
#   program     load the bitstream over JTAG and set up the ports (../program_fpga.sh), build
#               every app, check that the apps see the bitstream
#   locality    Fig. 1c, distinct flows per burst                      run_locality.sh
#   ndr         zero-loss throughput, 1 core and Partitioned 2/4 cores,
#               theoretical max                                        run_ndr.sh <app>
#   latency     latency against the queues, Pareto frontier            run_latency.sh <app>
#   starvation  flow completion time                                   run_starvation.sh <app>
#   adaptive    adaptive queue count under changing load and flows     run_adaptive.sh <app>
#   plot        every plot into plots/                                 plot_all.sh
#
# Each step runs for MICA and then for nf-chain (--apps selects). The measurements take days (the zero-loss runs of MICA
# alone took ~42 h): launch it in tmux (see README.md). Every step resumes: points
# already in results/ are skipped, so after an interruption the same command goes on.
# A step that fails does not stop the next.
#
#   ./run_all.sh                        # everything
#   ./run_all.sh --no-program ndr       # the NDR runs only, the bitstream is already loaded
#   ./run_all.sh --apps nf-chain latency starvation
#   ./run_all.sh --dry-run              # print every command, run nothing

. "$(dirname "$0")/lib.sh"
set +e

usage() { sed -n '2,/^$/{s/^# \{0,1\}//;p}' "$0"; exit "${1:-0}"; }

program=1
apps="mica nf-chain"
steps=()
while [ $# -gt 0 ]; do
	case $1 in
	--no-program) program=0 ;;
	--dry-run) export DRY_RUN=1 ;;
	--apps) apps=$2; shift ;;
	-h | --help) usage ;;
	program | locality | ndr | latency | starvation | adaptive | plot) steps+=("$1") ;;
	*) echo "unknown argument $1" >&2; usage 1 ;;
	esac
	shift
done
[ ${#steps[@]} -gt 0 ] || steps=(program locality ndr latency starvation adaptive plot)
for a in $apps; do
	[ "$a" = mica ] || [ "$a" = nf-chain ] || { echo "unknown app $a" >&2; exit 1; }
done
export TAG

failed=()
step() {
	echo "=== $(date -u '+%F %T') $*"
	"$@" || { echo "=== FAILED: $*"; failed+=("$*"); }
}

# no-drop-zip.py switches turbo off for its runs and restores it; the other scripts leave it as it is
check_turbo() {
	local boost
	boost=$(cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null)
	[ "$boost" = 1 ] || return 0
	for s in "${steps[@]}"; do
		case $s in latency | starvation | adaptive)
			echo "WARNING: turbo is on (cpufreq/boost = 1) and the $s step does not switch it off:" >&2
			echo "         echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost   (the paper's runs: fixed 3.1 GHz)" >&2
			return 0 ;;
		esac
	done
}

# the apps print "bitstream: <name> (...)" when they start (common/qbitstream.h)
check_bitstream() {
	local out
	[ -z "$DRY_RUN" ] || { echo "+ check that nf-chain prints 'bitstream: $TAG'"; return 0; }
	out=$(sudo timeout 90 stdbuf -oL ../nf-chain/build/nf-chain -d librte_net_qdma.so -n 4 -a 0000:16:00.1 -l 0 -- \
		-p 0x3 --config="(0,0,0)" --rule_ipv4=../nf-chain/rules/fw_10k --rule_ipv6=../nf-chain/rules/rule_ipv6.db \
		-q 1 2>&1 | grep -m1 '^bitstream:')
	echo "$out"
	[[ $out == "bitstream: $TAG "* ]]
}

program_step() {
	if [ $program = 1 ]; then
		run sudo ../program_fpga.sh "$BITSTREAM" || return 1
	fi
	run make -C ../nf-chain && run make -C ../mica && run make -C apps/starvation &&
		run make -C apps/adaptive && run make -C apps/locality || return 1
	check_bitstream
}

check_turbo
for s in "${steps[@]}"; do
	case $s in
	program)
		step program_step || { echo "bitstream or build not ready: nothing else is run" >&2; exit 1; } ;;
	locality) step ./run_locality.sh ;;
	plot) step ./plot_all.sh ;;
	*) for a in $apps; do step ./run_$s.sh "$a"; done ;;
	esac
done

echo "=== $(date -u '+%F %T') done, ${#failed[@]} failed steps"
printf '  %s\n' "${failed[@]}"
[ ${#failed[@]} -eq 0 ]
