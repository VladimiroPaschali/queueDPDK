# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Two DPDK applications whose throughput is measured while varying the number of RX queues polled per core (1 to 2048) and the queue-selection policy. The target hardware is an AMD Alveo U55N running a modified Open-NIC shell with the QDMA PMD. Comments are partly in Italian.

- `nf-chain/`: the paper's "NF chain", an ACL followed by a Count-Min Sketch and a NAT. The NAT code lives in `src/nat_*`, `state.*` and `utils/`. The ACL comes from l3fwd-acl and is driven by `rules/fw_10k` and `rules/rule_ipv6.db`.
- `mica/`: the paper's MICA key-value store. `ported-mica/` is the MICA table, and the workload alternates GET and SET.
- `common/qflow.h`: CLI options, validation and names of the policies, shared by both apps.
- `trex-rfc/no-drop-zip.py`: zero-loss throughput (RFC2544 NDR) with TRex on another host (`STLClient(server="100.78.72.16")`). Traffic comes from the `zipf-profile.py` profile. `pqos.py` collects LLC/memory-bandwidth data for the Discussion section.
- `locality/`: a separate CMS app plus `locality-bench.py` for Fig. 2 (distinct flows per burst). It does not use the policies.

## Build

```
cd nf-chain && make     # build/nf-chain
cd mica && make         # build/mica
```

The apps call QDMA PMD functions directly (`qdma_reg_read_usr`, `rte_pmd_qdma_*`). They can only be linked statically against the patched tree `../../dpdk-20.11` (`DPDK_LOCAL_PATH`). A `pkg-config` shared build fails at link time. There are no unit tests. Running on the FPGA is the only end-to-end check. Without the FPGA you can still smoke-test argument parsing: `sudo` is not needed with `--no-huge -m 256 --no-pci --vdev=net_null0 -l 0 -- <args>`. The run then crashes at the QDMA bitstream check, which is expected, and `stdbuf -oL` keeps the output.

## Policies and the polling loop

Policies are runtime options (see the table at the top of `common/qflow.h`): `--policy baseline|aggressive|penalty|aggressive+penalty`, `--penalty N`, `--latency-queue Q`, `--latency-period P` and `--multicore partitioned|shared`. In the code, Penalty is the variable `penalty` with the counters `queue_hit[]`, and Aggressive is `aggressive` with `max_loops`.

Each app has a single `poll_loop(const enum qflow_mode mode)` marked `__rte_always_inline`. It is instantiated three times (`main_loop_single`, `main_loop_partitioned`, `main_loop_shared`), so each multicore policy compiles to its own loop. The three modes intentionally keep the internals of the original separate programs, so the paper's numbers stay reproducible. Do not "unify" these differences without re-measuring:

- **Single core.** TX on queue 0 per burst, per-second `print_stats()`, and throughput measured after a 3 s warmup.
- **Multicore.** TX is batched (`TX_BATCH_SIZE`) on the lcore's own TX queue, with no per-second statistics.
- **Penalty counters.** Global on a single core, per lcore otherwise. Partitioned indexes them relative to its slice and keeps that index across a latency diversion.
- **nf-chain CMS.** 16×1M with a row-pointer table on a single core and in Shared, 16×65536 embedded in Partitioned, one sketch per lcore.
- **nf-chain NAT.** One table, one per lcore (Partitioned) or one per queue (Shared). Shared tables are locked and use 15-byte FlowIds (`FLOWID_PACKED_SIZE`). Multicore drops new flows when the table is full.
- **nf-chain mempool.** One per lcore in Partitioned, a single one otherwise.
- **mica.** Non-concurrent table with FIFO eviction on a single core. With more than one polling lcore the table is concurrent, and multicore uses an MTH threshold of 1.0. The single-core mempool omits the per-lcore cache term. Partitioned splits queues with `queues/lcores + extra` (nf-chain uses `queues*pos/lcores`).

The Python scripts parse the app output with regexes (`measured RX Throughput:`, `empty/sec (the poll read no packets)`, `pkts/burst`, and `ready` / `entering main loop` for startup). Keep those strings stable.

## Running

Use the FPGA at PCI `0000:16:00.1`, loading the PMD with `-d librte_net_qdma.so`. `ports.sh` rebinds the device and sets the Open-NIC registers. nf-chain needs `-p 0x3 --config="(0,0,<lcore>)" --rule_ipv4=... --rule_ipv6=...`, and MICA takes only `-q`, `-d`, `-v` and the policy options. At startup both apps send `log2(queues)+1` packets carrying a magic value to clear leftover state. A packet with the `STOP123456789` magic stops nf-chain.

Experiments: `cd trex-rfc && python3 no-drop-zip.py <experiment>...`. The experiment names are in the `EXPERIMENTS` dict (`nf-chain`, `mica`, `{nf-chain,mica}-{shared,partitioned}{2,4,8}`). For the latency test (Fig. 6), run `nf-chain -q 64 --policy aggressive --latency-queue 27` while TRex on the other host measures latency.

Results (`*.csv`), `perf.data` and `build/` are gitignored.
