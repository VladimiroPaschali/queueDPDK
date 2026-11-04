# queueDPDK

Code and experiments for the paper *Programmable NIC Polling for Software Packet Processing* (QFlow).

Two DPDK applications whose throughput, latency and cache behaviour are measured while varying the number of RX queues polled per core (1 to 2048) and the queue-selection policy, on an AMD Alveo U55N running a modified Open-NIC shell with the QDMA PMD. Traffic comes from Cisco TRex on a second host.

## Layout

| Path | Contents |
|---|---|
| `nf-chain/` | Use case 1: ACL + Count-Min Sketch + NAT chain |
| `mica/` | Use case 2: MICA key-value store, 50% GET / 50% SET |
| `common/` | `qflow.h` (policy options shared by the apps), `qbitstream.h` (which bitstream is loaded), and the headers shared by the variants: `qprobe.h` (poll log of the starvation runs), `adaptive.h` + `qdma_rss.[ch]` (adaptive queue count) |
| `experiments/` | `run_all.sh`, the step scripts, the TRex campaigns (`no-drop-zip.py`, `latency.py`, `starvation.py`, `adaptive.py`), the plot scripts, and `apps/` with the other applications of the paper (`locality`, `starvation`, `adaptive`) |
| `patches/dpdk-20.11-qdma.patch` | changes to DPDK 20.11 + the QDMA driver (see Setup) |
| `extended_table_prefetch.{bit,mcs}` | the Open-NIC bitstream of the paper: 2048 RX queues (RSS table extended), C2H prefetch cache of 64 queues. The apps print it as `bitstream: pfch64` |
| `program_fpga.sh`, `ports.sh` | load the bitstream over JTAG, bind the device to vfio-pci and set the Open-NIC registers (`ports.sh`: the same without programming) |

## Setup

**DPDK tree.** The apps link statically against a patched DPDK 20.11 tree in `../../dpdk-20.11` (override with `DPDK_LOCAL_PATH=`), because they call the QDMA PMD directly (`qdma_reg_read_usr`, `rte_pmd_qdma_*`).

```
# 1. QDMA driver patched for Open-NIC (see the Open-NIC DPDK repository)
git clone https://github.com/Xilinx/dma_ip_drivers && cd dma_ip_drivers && git checkout 7859957
cp <open-nic-dpdk>/*.patch . && git apply *.patch && cd ..
# 2. DPDK 20.11 with that driver
tar xf dpdk-20.11.tar.xz && cd dpdk-20.11
cp -R ../dma_ip_drivers/QDMA/DPDK/drivers/net/qdma drivers/net
# 3. this repository's changes: 'qdma' in drivers/net/meson.build, RTE_MAX_QUEUES_PER_PORT 2048
#    (config/rte_config.h), the user-register access, the RETA that writes the RSS mask,
#    RX doorbell every 32 packets, QDMA devices in usertools/dpdk-devbind.py
patch -p1 < <queueDPDK>/patches/dpdk-20.11-qdma.patch
meson build && ninja -C build
```

The patch is against the tree of steps 1-2 (DPDK 20.11 plus the driver at that commit with the five Open-NIC patches). Applied to it, `drivers/net/qdma`, `drivers/net/meson.build`, `config/rte_config.h` and `usertools/dpdk-devbind.py` are exactly those of the tree the paper's results were measured on (checked file by file). The apps need nothing else: the tree also has a VTune hook in `lib/librte_eventdev` and a Count-Min Sketch version of `examples/l2fwd`, which the patch does not include.

**Host.** Ubuntu 24.04, kernel 6.8, Xeon W5-3435X with turbo off (fixed 3.1 GHz). The zero-loss runs (`no-drop-zip.py`) switch turbo off themselves and restore it; `latency.py`, `starvation.py` and `adaptive.py` only set the `performance` governor and leave turbo as it is, so before those steps turn it off (`echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost`): `run_all.sh` warns when it is on. The results of the paper were measured with `isolcpus=0-11 nohz_full=0-11 rcu_nocbs=0-11 nosmt`, 20 1 GB hugepages from `/dev/hugepages` (a `pagesize=1G` mount) for the EAL and, for MICA, a `hugetlbfs` mount on `/mnt/huge` with `pagesize=2M` and about 4096 free 2 MB pages (`echo 4096 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages`). The mounts and the pages do not survive a reboot. Python 3 with `tqdm`, `numpy`, `pandas` and `matplotlib`.

**TRex.** A TRex server (v3.08, stateless) on another host, at the address in the scripts (`TREX_SERVER`, `100.78.72.16`), and its Python API in `/trex/v3.08/automation/trex_control_plane/interactive` on this host.

**FPGA.** `sudo ./program_fpga.sh` loads `extended_table_prefetch.bit` with `xsdb` (Vitis 2024.1) and rescans the PCIe functions; with `--no-program` it only binds and sets the registers (after a reboot with the bitstream loaded). To regenerate the `.mcs` of a bitstream (flash image of the u55n, 128 MB, SPIx4, bitstream at 0x01002000, as the Open-NIC build does):

```
vivado -mode batch -nojournal -nolog -source mkmcs.tcl -tclargs extended_table_prefetch.bit extended_table_prefetch.mcs
# mkmcs.tcl:  write_cfgmem -format mcs -size 128 -interface SPIx4 \
#                 -loadbit "up 0x01002000 [lindex $argv 0]" -force -file [lindex $argv 1]
```

## Build

```
cd nf-chain && make               # build/nf-chain
cd mica && make                   # build/mica-fifo-nocopy-{prefetch,pools-prefetch,prefetch-lat,prefetch-qprobe,prefetch-adaptive}
cd experiments/apps/starvation && make   # build/starvation (nf-chain + poll log)
cd experiments/apps/adaptive && make     # build/adaptive (nf-chain + adaptive queue count)
cd experiments/apps/locality && make     # build/cms (distinct flows per burst)
```

## Policies

| Paper | Option |
|---|---|
| Round-Robin (baseline) | `--policy baseline` (default) |
| Aggressive | `--policy aggressive` |
| Penalty | `--policy penalty` (`--penalty N`, default 5) |
| Aggressive + Penalty (QFlow) | `--policy aggressive+penalty` |
| Latency | `--latency-queue Q` (`--latency-period P`, default 4), on top of any policy |
| Partitioned | `--multicore partitioned` |
| Adaptive | `--adaptive` (`experiments/apps/adaptive` and `mica-fifo-nocopy-prefetch-adaptive`, single core) |

With more than one lcore `--multicore partitioned` is required. `-q N` sets the number of RX queues (a power of 2) and `-d N` the descriptors per queue.

```
sudo nf-chain/build/nf-chain -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- \
    -p 0x3 --config="(0,0,4)" \
    --rule_ipv4=nf-chain/rules/fw_10k --rule_ipv6=nf-chain/rules/rule_ipv6.db \
    -q 64 --policy aggressive

sudo mica/build/mica-fifo-nocopy-pools-prefetch -d librte_net_qdma.so -l 0-3 -n 4 -a 0000:16:00.1 -- \
    -q 256 --multicore partitioned --policy aggressive+penalty
```

## Running the paper's tests

`experiments/run_all.sh` runs every test of the paper on the bitstream above, MICA first and then nf-chain, and then plots. It takes days (the zero-loss runs of MICA alone took about 42 hours), so run it in tmux; it resumes, skipping what is already in `experiments/results/`.

```
cd experiments
tmux new-session -d -s paper "trap : INT; ./run_all.sh 2>&1 | tee -ia logs/run_all.log; exec bash"
./run_all.sh --dry-run                  # print every command and run nothing
./run_all.sh --no-program ndr           # one step, bitstream already loaded
./run_all.sh --apps nf-chain latency starvation
```

The `trap : INT` and `tee -i` keep a C-c from killing the shell around the Python script, which must see SIGINT: its `finally` frees the TRex port and stops the app.

| Figure | Step | Script | Results (`experiments/results/`) | Plot (`experiments/plots/`) |
|---|---|---|---|---|
| 1c: distinct flows per burst | `locality` | `run_locality.sh` | `locality/` | (`locality_summary.csv`) |
| 3: motivation (L1 misses, throughput, empty polls) | `ndr` | `run_ndr.sh nf-chain` | `ndr/results_nf-chain-pfch64_skew*` | `ndr/pfch64/` |
| single-core throughput and L1 misses, with the theoretical max | `ndr` | `run_ndr.sh <app>` | `ndr/results_{nf-chain,mica-fifo-nocopy-prefetch}-pfch64_skew*`, `…-seq-*-pfch64_skew*` | `ndr/pfch64/`, `bound/pfch64/` |
| multicore (Partitioned, 1/2/4 cores) | `ndr` | `run_ndr.sh <app>` | `ndr/results_*-partitioned{2,4}-pfch64_skew*` | `ndr/pfch64/` |
| latency against the queues, Pareto frontier | `latency` | `run_latency.sh <app>` | `latency/results_latency_{nf-chain,mica-prefetch}-pfch64_skew0.6` | `latency/` |
| starvation (flow completion time) | `starvation` | `run_starvation.sh <app>` | `starvation/<app>-pfch64` | `starvation/` |
| adaptive (changing load, changing flows) | `adaptive` | `run_adaptive.sh <app>` | `adaptive/<app>-pfch64` | `adaptive/` |

Two measurements of the paper have no script: the 16 ns per queue of the Motivation (an L2 forwarding application at 500 pps, with 1 to 2048 queues) and the LLC-to-DRAM traffic of the Discussion, taken with `experiments/pqos.py` (Intel PQoS) while an app runs.

Underneath, `no-drop-zip.py <experiment>...` finds the zero-loss rate (RFC 2544) with TRex for every queue count, policy and Zipf skew (0.1 near-uniform, 0.6 low, 0.9 high), and takes `perf stat` windows on the same process at that rate. `--queues/--policies/--skews/--repetitions` override an experiment, `--tag=-x` writes `results_<experiment>-x_skew*` (the `=` is needed), `--resume` skips the points already measured; without `--resume` the CSVs of the experiment's folder are deleted. The experiments are in the `EXPERIMENTS` dict of the script.

`latency.py`, `starvation.py` and `adaptive.py` take `--help`; they do not touch turbo (see Setup).
