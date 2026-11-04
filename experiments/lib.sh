# Sourced by the run_*.sh and plot_all.sh scripts of experiments/.
#
#   TAG        name of the bitstream the results belong to. It is the name the apps
#              print (common/qbitstream.h: "bitstream: pfch64") and the suffix of the
#              results folders (results_<experiment>-<TAG>_skew*)
#   DRY_RUN=1  print the commands instead of running them (run_all.sh --dry-run)
#
# A C-c reaches the python script, which stops the app and frees the TRex port in its
# finally; the shell then stops here (run campaigns in tmux, see README.md).

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
trap 'exit 130' INT
set -e

TAG=${TAG:-pfch64}
BITSTREAM=$PWD/../extended_table_prefetch.bit
ALL_QUEUES="1 2 4 8 16 32 64 128 256 512 1024 2048"

# run <command...>: print it and run it (not with DRY_RUN)
run() {
	printf '+'
	printf ' %q' "$@"
	echo
	[ -n "$DRY_RUN" ] || "$@"
}

# MICA takes its table from the 2 MB hugepages in /mnt/huge: files of runs that were
# stopped keep the pages
mica_cleanup() {
	pgrep -f 'build/mica' >/dev/null || run sudo sh -c 'rm -f /mnt/huge/mehcached_shm_*'
}

# app_names <app>: the experiment names of run_ndr.sh and the NDR results of the app
# sets SINGLE PART BOUND
app_names() {
	case $1 in
	mica)
		SINGLE=mica-fifo-nocopy-prefetch
		PART=mica-fifo-nocopy-pools-prefetch-partitioned
		BOUND=mica-fifo-nocopy-prefetch-seq-trex2 ;;
	nf-chain)
		SINGLE=nf-chain
		PART=nf-chain-partitioned
		BOUND=nf-chain-seq-ring8192 ;;
	*)
		echo "unknown app $1 (mica or nf-chain)" >&2
		exit 1 ;;
	esac
}
