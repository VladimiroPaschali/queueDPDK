"""
locality-bench.py

Lancia TRex con profilo Zipf a throughput fisso, avvia l'app DPDK locality
per catturare le 5-tuple per queue, poi esegue la flow analysis offline.

Parametri via CLI:
  --queues   lista di num. code  (default: 1 8 64 256 512)
  --skews    lista di skew Zipf  (default: 0.1 0.6 0.9)
  --pps      throughput fisso    (default: 1000000)
  --flows    numero flussi Zipf  (default: 10000)
  --size     dimensione pacchetto(default: 64)
  --keep-raw conserva il file raw dopo l'analisi

Uso:
  python3 locality-bench.py
  python3 locality-bench.py --queues 1 64 256 --skews 0.6 0.9 --pps 500000
"""

import argparse
import csv
import math
import os
import shlex
import subprocess as sp
import sys
import threading
from collections import defaultdict
from time import sleep

from tqdm import tqdm

sys.path.append("/trex/v3.08/automation/trex_control_plane/interactive")
from trex.stl.api import STLClient, STLProfile

# ── Paths ─────────────────────────────────────────────────────────────────────

PROFILE_FILE  = os.path.join(os.path.dirname(__file__), "../trex-rfc/zipf-profile.py")
DPDK_BASE_CMD = (
    "sudo /home/vladimiro/dpdk_patched/queueDPDK/locality/build/cms"
    " -d librte_net_qdma.so -l 4 -n 4 -a 16:00.1 --"
    " -P 1 -T 1 -d 1024 -c 1048576 -p 0"
)
TREX_SERVER   = "100.78.72.16"
PORTS         = [0]

# ── Analysis (da locality/analysis/locality.py) ───────────────────────────────

def burst_analysis(filename: str, output_csv: str) -> int:
    """Raggruppa le righe per burst_id (col 0) e calcola per ogni burst:
    flussi distinti, max pacchetti consecutivi dello stesso flusso, totale.
    Formato riga atteso: burst_id,queue,src_ip,dst_ip,src_port,dst_port,proto
    Restituisce il numero di burst trovati."""
    bursts = []
    prev_burst_id = None
    current_queue = None
    flows_in_burst: set = set()
    packets_in_burst = 0
    prev_flow = None
    consecutive_count = 0
    max_consecutive = 0

    with open(filename, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            if not row[0].strip().isdigit():
                continue
            if len(row) < 7:
                continue
            burst_id = int(row[0])
            queue    = int(row[1])
            flow     = tuple(row[2:])  # src_ip, dst_ip, src_port, dst_port, proto

            if prev_burst_id is not None and burst_id != prev_burst_id:
                max_consecutive = max(max_consecutive, consecutive_count)
                bursts.append({
                    "burst": prev_burst_id,
                    "coda": current_queue,
                    "flussi": len(flows_in_burst),
                    "pacchetti_consecutivi": max_consecutive,
                    "pacchetti_totali": packets_in_burst,
                })
                flows_in_burst = set()
                packets_in_burst = 0
                consecutive_count = 0
                max_consecutive = 0
                prev_flow = None

            flows_in_burst.add(flow)
            packets_in_burst += 1
            consecutive_count = consecutive_count + 1 if flow == prev_flow else 1
            max_consecutive = max(max_consecutive, consecutive_count)
            current_queue = queue
            prev_burst_id = burst_id
            prev_flow = flow

    if prev_burst_id is not None:
        max_consecutive = max(max_consecutive, consecutive_count)
        bursts.append({
            "burst": prev_burst_id,
            "coda": current_queue,
            "flussi": len(flows_in_burst),
            "pacchetti_consecutivi": max_consecutive,
            "pacchetti_totali": packets_in_burst,
        })

    with open(output_csv, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["burst", "coda", "flussi", "pacchetti_consecutivi", "pacchetti_totali"]
        )
        writer.writeheader()
        writer.writerows(bursts)

    return len(bursts)


def flow_analysis(input_csv: str, output_csv: str) -> dict:
    """Calcola statistiche aggregate per coda a partire dal CSV dei burst.
    Restituisce un dict con le medie globali."""
    bursts_per_queue: dict = defaultdict(list)

    with open(input_csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            queue = int(row["coda"])
            flows = int(row["flussi"])
            max_consec = int(row["pacchetti_consecutivi"])
            bursts_per_queue[queue].append((flows, max_consec))

    def _mean(lst):
        return sum(lst) / len(lst) if lst else 0.0

    def _std(lst):
        if len(lst) < 2:
            return 0.0
        m = _mean(lst)
        return math.sqrt(sum((x - m) ** 2 for x in lst) / (len(lst) - 1))

    queue_stats = []
    all_flows: list = []
    all_consec: list = []

    for queue, data in sorted(bursts_per_queue.items()):
        flows_list  = [x[0] for x in data]
        consec_list = [x[1] for x in data]
        all_flows.extend(flows_list)
        all_consec.extend(consec_list)
        queue_stats.append({
            "coda": queue,
            "flussi_media":           _mean(flows_list),
            "flussi_std":             _std(flows_list),
            "pacchetti_consec_media": _mean(consec_list),
            "pacchetti_consec_std":   _std(consec_list),
        })

    with open(output_csv, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["coda", "flussi_media", "flussi_std",
                        "pacchetti_consec_media", "pacchetti_consec_std"],
        )
        writer.writeheader()
        writer.writerows(queue_stats)

    return {
        "flows_mean":  _mean(all_flows),
        "flows_std":   _std(all_flows),
        "consec_mean": _mean(all_consec),
        "consec_std":  _std(all_consec),
    }


# ── Summary CSV ───────────────────────────────────────────────────────────────

def append_summary(summary_csv: str, skew: float, queue: int, pps: int,
                   n_bursts: int, stats: dict) -> None:
    exists = os.path.isfile(summary_csv)
    with open(summary_csv, "a", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["skew", "queue", "pps", "bursts",
                        "flows_mean", "flows_std", "consec_mean", "consec_std"],
        )
        if not exists:
            writer.writeheader()
        writer.writerow({
            "skew":        skew,
            "queue":       queue,
            "pps":         pps,
            "bursts":      n_bursts,
            "flows_mean":  round(stats["flows_mean"], 4),
            "flows_std":   round(stats["flows_std"],  4),
            "consec_mean": round(stats["consec_mean"], 4),
            "consec_std":  round(stats["consec_std"],  4),
        })


# ── CPU governor ──────────────────────────────────────────────────────────────

def set_governor(governor: str) -> None:
    for core in range(9):
        path = f"/sys/devices/system/cpu/cpu{core}/cpufreq/scaling_governor"
        try:
            sp.run(["sudo", "tee", path], input=governor, text=True,
                   stdout=sp.DEVNULL, check=True)
        except sp.CalledProcessError:
            pass


# ── TRex helpers ──────────────────────────────────────────────────────────────

def trex_start(client: STLClient, pps: int) -> None:
    client.start(ports=PORTS, mult=f"{pps}pps", force=True)
    tqdm.write(f"Waiting for TRex to reach {pps:,} pps...")
    while True:
        current = client.get_stats(ports=PORTS)[0]["tx_pps"]
        if current >= pps * 0.9:
            tqdm.write(f"TRex stable at {current:,.0f} pps")
            break
        sleep(1)


def trex_stop(client: STLClient) -> None:
    tqdm.write("Stopping TRex...")
    client.stop(ports=PORTS)
    while client.get_stats(ports=PORTS)[0]["tx_pps"] >= 2:
        sleep(1)
    tqdm.write("TRex stopped.")


# ── DPDK helpers ──────────────────────────────────────────────────────────────

def dpdk_launch(queue: int, raw_output_file: str):
    """Lancia l'app DPDK locality con stdout in PIPE.
    Un thread scrive l'output su file e imposta il ready event quando vede
    'entering main loop' (RTE_LOG va su stdout in DPDK, non stderr)."""
    command = f"{DPDK_BASE_CMD} -q {queue}"
    tqdm.write(f"Launching DPDK: {command}")

    process = sp.Popen(
        shlex.split(command),
        stdout=sp.PIPE,
        stderr=sp.DEVNULL,
        text=True,
        bufsize=1,
    )

    ready = threading.Event()

    def _read_stdout() -> None:
        with open(raw_output_file, "w", buffering=1 << 20) as f_out:
            for line in process.stdout:  # type: ignore[union-attr]
                f_out.write(line)
                if not ready.is_set() and "entering main loop" in line.lower():
                    ready.set()

    threading.Thread(target=_read_stdout, daemon=False).start()

    if ready.wait(timeout=30):
        tqdm.write("DPDK app ready (main loop started)")
    else:
        tqdm.write("WARNING: DPDK ready timeout, continuing anyway")

    return process


def dpdk_wait(process) -> None:
    """Attende la fine dell'app DPDK (si autotermina dopo ~13 s)."""
    tqdm.write("Waiting for DPDK app to finish (~13 s)...")
    try:
        process.wait(timeout=60)
    except sp.TimeoutExpired:
        tqdm.write("WARNING: DPDK timeout, terminating")
        process.terminate()
        process.wait()
    tqdm.write("DPDK app done.")


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Locality benchmark: TRex Zipf fisso + DPDK locality app + flow analysis"
    )
    p.add_argument("--queues", nargs="+", type=int,
                   default=[1,2,4,8,16,32,64,128,256,512,1024,2048],
                   metavar="N",
                   help="Lista di numero di code da testare (default: 1 2 4 8 16 32 64 128 256 512 1024 2048)")
    p.add_argument("--skews", nargs="+", type=float,
                   default=[0.1, 0.6, 0.9],
                   metavar="S",
                   help="Lista di skew Zipf (default: 0.1 0.6 0.9)")
    p.add_argument("--pps", type=int,
                   default=30_000_000,
                   metavar="PPS",
                   help="Throughput fisso in pacchetti/secondo (default: 30000000)")
    p.add_argument("--flows", type=int,
                   default=10_000,
                   metavar="N",
                   help="Numero di flussi Zipf (default: 10000)")
    p.add_argument("--size", type=int,
                   default=64,
                   metavar="B",
                   help="Dimensione pacchetto in byte (default: 64)")
    p.add_argument("--keep-raw", action="store_true",
                   help="Conserva il file raw dei pacchetti dopo l'analisi")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    queues_list = args.queues
    zipf_skews  = args.skews
    throughput  = args.pps
    num_flows   = args.flows
    pkt_size    = args.size
    keep_raw    = args.keep_raw

    tqdm.write(f"Queues:     {queues_list}")
    tqdm.write(f"Skews:      {zipf_skews}")
    tqdm.write(f"Throughput: {throughput:,} pps")
    tqdm.write(f"Flows:      {num_flows:,}  |  Pkt size: {pkt_size} B")

    summary_csv = "locality_summary.csv"
    if os.path.isfile(summary_csv):
        os.remove(summary_csv)

    client = STLClient(server=TREX_SERVER)

    try:
        tqdm.write("Connecting to TRex...")
        client.connect()
        set_governor("performance")

        total = len(zipf_skews) * len(queues_list)
        pbar  = tqdm(total=total, desc="locality", unit="test")

        for skew in zipf_skews:
            tqdm.write(f"\n{'='*60}")
            tqdm.write(f"Zipf skew: {skew}  |  throughput: {throughput:,} pps")
            tqdm.write(f"{'='*60}")

            # Carica gli stream una volta per skew
            client.reset(ports=PORTS)
            tqdm.write(f"Loading {num_flows:,} Zipf streams (skew={skew})...")
            streams = STLProfile.load_py(
                PROFILE_FILE,
                direction=0,
                port_id=0,
                num_flows=num_flows,
                skew=skew,
                packet_size=pkt_size,
            ).get_streams()
            tqdm.write(f"Loaded {len(streams):,} streams")
            client.add_streams(streams, ports=PORTS)

            for queue in queues_list:
                pbar.set_description(f"[skew:{skew:.2f} | Q:{queue:4d}]")

                results_dir = os.path.join("results_locality", f"skew{skew}_q{queue}")
                os.makedirs(results_dir, exist_ok=True)

                raw_file   = os.path.join(results_dir, "flows_raw.txt")
                bursts_csv = os.path.join(results_dir, "bursts.csv")
                flow_csv   = os.path.join(results_dir, "flow_stats.csv")

                # 1. Avvia TRex a throughput fisso
                trex_start(client, throughput)

                # 2. Lancia DPDK (si autotermina dopo ~13 s)
                process = dpdk_launch(queue, raw_file)
                dpdk_wait(process)

                # 3. Ferma TRex
                trex_stop(client)

                # 4. Analisi offline
                tqdm.write("Running burst analysis...")
                n_bursts = burst_analysis(raw_file, bursts_csv)
                tqdm.write(f"  {n_bursts:,} bursts")

                tqdm.write("Running flow analysis...")
                stats = flow_analysis(bursts_csv, flow_csv)
                tqdm.write(
                    f"  flows/burst:  {stats['flows_mean']:.2f} ± {stats['flows_std']:.2f}"
                )
                tqdm.write(
                    f"  consec/burst: {stats['consec_mean']:.2f} ± {stats['consec_std']:.2f}"
                )

                # 5. Riga nel CSV sommario globale
                append_summary(summary_csv, skew, queue, throughput, n_bursts, stats)

                # 6. Opzionale: rimuovi raw per risparmiare spazio
                if not keep_raw:
                    os.remove(raw_file)

                pbar.update(1)

        pbar.close()
        tqdm.write(f"\nRisultati salvati in results_locality/  |  sommario: {summary_csv}")

    finally:
        set_governor("schedutil")
        tqdm.write("Disconnecting from TRex...")
        client.disconnect()


if __name__ == "__main__":
    main()
