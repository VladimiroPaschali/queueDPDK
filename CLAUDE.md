# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A research testbed of standalone DPDK applications that measure how per-core packet-processing throughput and cache behaviour change with the **number of RX queues one core polls** (1 → 2048), and with different queue-polling policies. It was first built for the Xilinx Open-NIC FPGA (QDMA PMD, see https://github.com/Xilinx/open-nic-dpdk); some variants now target a Mellanox ConnectX-6 Dx (mlx5 PMD). Comments and docs are often in Italian.

Each top-level directory is an **independent app with its own `Makefile` and `main.c`/`src/`**. No code is shared between apps; variants are copy-and-modify forks. When you fix something, check whether sibling variants need the same fix.

- `cms`, `rfc_cms`, `locality`, `latency`: Count-Min Sketch NF (`-c` = CMS columns)
- `chain`, `rfc_chain`, `l3fwd-acl`: ACL/forwarding chain (uses `fw_10k`, `rule_ipv*.db`)
- `asni-nat*`: NAT + CMS + ACL NF (`src/` with `nat_*`, `nf-*`, `state.*`). Variants:
  - `asni-nat`: FPGA/QDMA, single core
  - `asni-nat-multicore`: queues split into contiguous ranges per lcore (`queues*pos/total_lcores`), with per-core CMS (see `asni-nat-multicore/CHANGES.md`)
  - `asni-nat-multicore-corec`: every lcore polls every queue, guarded by `rte_spinlock_trylock` per queue
  - `asni-nat-mlx`: port to Mellanox (QDMA code removed, standard `rte_eth_rx_queue_setup`, RSS RETA round-robin). `QUEUE_SWITCHING_ANALYSIS.md` compares queue-switch cost across QDMA and mlx5 drivers
  - `asni-nat-mlx-25`: the same app built against DPDK 25.11 (Meson, `/home/vladimiro/ucl/dpdk-25.11`)
- `toasty-mica*`: MICA key-value store (`ported-mica/`) as an NF, with the same multicore/corec split
- `base`: minimal multi-RX/TX skeletons
- `fastclick`, `enso_eval`, `toasty`, `mica2OLD`: vendored third-party/comparison code. Leave these alone unless asked.

## Shared polling-loop semantics

All apps share the same main-loop structure: `for (i = 0; i < queues; ++i) rte_eth_rx_burst(port, i, ...)`, process the burst, then TX all packets ("no drop" policy). The common app flags are:
- `-q N`: number of RX queues polled by the core (power of 2 in experiments)
- `-d N`: RX descriptors per queue (power of 2, ≥64)
- `-a`: **aggressive** policy. If a burst comes back full (`MAX_PKT_BURST`), poll the same queue again, at most 100 times in a row
- `-s K`: **skip** policy. A queue whose last burst was under half full is skipped for the next K iterations (`queue_hit[]`)
- `-p`: portmask in asni-nat/chain, but prefetch distance in cms. Check `short_options[]` in each app.

Apps print throughput, empty-poll and packets-per-burst stats on exit. The Python harnesses parse those lines with regexes, so **changing these printf formats breaks `ez-test/dpdk*.py` and `trex-rfc/*.py`**.

## Building

DPDK sources live **outside** the repo in `../` (`../dpdk-20.11` is patched for QDMA, `../dpdk-20.11-original` is stock). The system `pkg-config libdpdk` is 20.11.0.

```
make            # 'shared' build (pkg-config libdpdk) -> build/<app>-shared, symlinked as build/<app>
make local      # static link against ../../dpdk-20.11[-original]/build (DPDK_LOCAL_PATH overridable)
make clean
```
`asni-nat-mlx-25` defaults to `local` against DPDK 25.11.

**Important for mlx builds:** `DPDK_INCLUDES` must put `-I$(DPDK_LOCAL_PATH)/config` first. Otherwise the system `rte_config.h` (`RTE_MAX_QUEUES_PER_PORT=2048`) does not match the static libs (1024), and inline `rte_eth_tx_burst` crashes because struct offsets differ. QDMA builds exclude the mlx drivers; mlx builds link `-lmlx5 -ljansson`.

There are no unit tests and no linter. Validation means running on hardware.

## Running

Apps need root, hugepages (`toasty-mica/setup.sh` mounts 2M hugepages) and a NIC bound to vfio-pci.
- FPGA (Open-NIC): PCI `0000:16:00.x`. The PMD is loaded with `-d librte_net_qdma.so`. `ports.sh` rebinds the device and pokes the Open-NIC registers through pcimem.
- Mellanox CX-6 Dx: PCI `0000:34:00.1`. Use no `-d`. Use only `-p 0x1` because there is one port. Devargs such as `mprq_en=1,rxqs_min_mprq=1` are optional. ethtool reports 16 channels, so start with `-q 16`. Intel IOMMU must be off for DPDK 20.11 mlx5.

Example:
```
sudo ./build/asni-nat -l 4 -n 4 -a 0000:34:00.1 -- -p 0x1 --config="(0,0,4)" \
    --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db -q 16 -a
```

## Experiment harnesses

- **`ez-test/`** (click CLI, needs sudo): `python main.py show`, `python main.py create --name X`, `sudo python main.py run --name <suite>`. Each suite is `suites/<name>/conf.json`, which sets `exp-dir`, `progs[].path/dpdk-args/app-args`, and sweep lists (`queues`, `descriptors`, `cms`, `aggressive`, `prefetch`, `llc-ways`, `perf` events). `"type": "dpdk"` (`dpdk.py`) wraps runs in `perf stat -C <cpu>`. `"type": "dpdk-rfc"` (`dpdk_rfc.py`) runs with external traffic. Which flags are appended depends on substring matches in the program `name` ("marco", "cms", "asni", "chain"). Results go to `results.csv` and `all_results.csv`.
- **`trex-rfc/no-drop-zip.py`**: an RFC2544-style NDR search using a TRex server (`STLClient(server="100.78.72.16")`, API at `/trex/v3.08/...`) and the Zipf traffic profile `zipf-profile.py`. Experiments are selected by editing the `commands_config` dict in `main()` (uncomment an entry: `base_command`, `policies`, `queues`, `zipf_skews`, `repetitions`). Output goes to `results_<name>_skew<s>/`. `pqos.py` handles LLC way allocation (CAT).

CSV, txt, log and `perf.data` files are gitignored, so results are not tracked.
