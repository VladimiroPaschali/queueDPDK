# queueDPDK

Code and experiments for the paper *Programmable NIC Polling for Software Packet Processing* (QFlow).

The applications run on an AMD Alveo U55N with the Open-NIC shell, extended to expose up to 2048 RX queues. For the environment (DPDK 20.11, QDMA driver, bitstream) see the [Open-NIC DPDK repository](https://github.com/Xilinx/open-nic-dpdk). The patches in this repository apply to that tree.

## Layout

| Path | Contents |
|---|---|
| `nf-chain/` | Use case 1: ACL + Count-Min Sketch + NAT chain |
| `mica/` | Use case 2: MICA key-value store, 50% GET / 50% SET |
| `common/qflow.h` | Queue-selection policy options shared by both applications |
| `trex-rfc/` | Zero-loss (RFC2544 NDR) throughput runs with TRex on a separate host |
| `locality/` | Fig. 2: captures per-queue 5-tuples to count distinct flows per burst |
| `extended_table.{bit,mcs}`, `ports.sh` | Open-NIC bitstream and port/register setup |

## Build

```
cd nf-chain && make      # -> nf-chain/build/nf-chain
cd mica && make          # -> mica/build/mica
```

Both build statically against the patched tree in `../../dpdk-20.11` (override with `DPDK_LOCAL_PATH=`).

## Policies

| Paper | Option |
|---|---|
| Baseline | `--policy baseline` (default) |
| Aggressive | `--policy aggressive` |
| Penalty | `--policy penalty` (`--penalty N`, default 5) |
| Aggressive + Penalty | `--policy aggressive+penalty` |
| Latency | `--latency-queue Q` (`--latency-period P`, default 4), on top of any policy |
| Partitioned | `--multicore partitioned` |
| Shared | `--multicore shared` |

With more than one lcore, `--multicore` is required. `-q N` sets the number of RX queues and `-d N` the descriptors per queue.

Examples:

```
sudo nf-chain/build/nf-chain -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- \
    -p 0x3 --config="(0,0,4)" \
    --rule_ipv4=nf-chain/rules/fw_10k --rule_ipv6=nf-chain/rules/rule_ipv6.db \
    -q 64 --policy aggressive

sudo mica/build/mica -d librte_net_qdma.so -l 0-3 -n 4 -a 0000:16:00.1 -- \
    -q 256 --multicore shared --policy aggressive+penalty
```

## Experiments

`trex-rfc/no-drop-zip.py` finds the zero-loss rate with TRex for each queue count, policy and Zipf skew (0.1 near-uniform, 0.6 low, 0.9 high), then reruns at that rate under `perf stat`. Results go to `trex-rfc/results_<experiment>_skew<s>/`.

```
cd trex-rfc
python3 no-drop-zip.py nf-chain            # Fig. 3, 5
python3 no-drop-zip.py mica                # Fig. 8
python3 no-drop-zip.py nf-chain-shared2 nf-chain-shared4     # Fig. 7
python3 no-drop-zip.py mica-shared2 mica-shared4             # Fig. 9
python3 no-drop-zip.py mica-shared2 mica-partitioned2        # Fig. 10
```

For the latency test (Fig. 6), run `nf-chain` with `-q 64 --policy aggressive --latency-queue 27` while TRex on the other host measures latency at a fixed rate.
