import argparse
import subprocess as sp
import shlex
import sys
from time import sleep
import time
import csv
import json
import os
import threading
import re
import statistics
from tqdm import tqdm
sys.path.append("/trex/v3.08/automation/trex_control_plane/interactive")
from trex.stl.api import STLClient, STLProfile
from trex.examples.stl.ndr_bench import NdrBench, NdrBenchConfig

PROFILE_FILE = "zipf-profile.py"  # generatore Zipf con PPS mode (più stabile)

PORTS = [0]  # single port loopback

def test_loopback(client):

    tqdm.write("Test veloce loopback (sanity check)...")
    client.start(ports=PORTS, mult="10pps", duration=5, force=True)
    client.wait_on_traffic(ports=PORTS)
    stats = client.get_stats()[0]
    tx = stats["opackets"]
    rx = stats["ipackets"]
    tqdm.write(f"TX={tx} RX={rx}")
    if rx == 0:
        raise RuntimeError("❌ Nessun traffico di ritorno! Loopback NON funzionante")

    # if abs(tx - rx) > 10:
    #     raise RuntimeError(f"❌ Differenza TX/RX troppo alta: {tx-rx}")

    tqdm.write("✅ Loopback OK\n")

def _parse_perf_output(perf_output: str) -> dict:

    perf_data = {}
    for line in perf_output.strip().splitlines():
    # Usa una regex per catturare numero ed evento
        match = re.match(r'\s*([\d,]+)\s+([^\s#(]+)', line)
        if match:
            number = match.group(1).replace(',', '')
            event = match.group(2)
            perf_data[event] = int(number)
    return perf_data

def _get_dpdk_throughput(dpdk_output) -> int:

    match = re.search(r'measured RX Throughput:\s*(\d+)', dpdk_output)
    if match:
        rx_packets = int(match.group(1))
        # tqdm.write(f"Throughput: {rx_packets}")
    else:
        rx_packets = -1
        tqdm.write("RX packets not found.")
    
    return rx_packets

def _get_empty_poll(dpdk_output) -> int:
    matches = re.findall(
        r'empty/sec\s*\(the poll read no packets\)\s*:\s*(\d+)',
        # r'empty/burst.*?:\s*([\d.]+)',
        dpdk_output,
        re.IGNORECASE
    )

    if matches:
        samples = [float(m) for m in matches[1:-1]] or [float(m) for m in matches]
        empty_poll = sum(samples) / len(samples)
        # print("Empty poll (ultimi 10):", last_10)
        # print("Empty poll (media):", empty_poll)
    else:
        empty_poll = ''  # multicore prints no per-second stats

    return empty_poll

def _get_packet_per_burst(dpdk_output) -> int:
    matches = re.findall(
        # r'packets/burst:\s*(\d+)',
        r'pkts/burst.*?:\s*([\d.]+)',
        dpdk_output,
        re.IGNORECASE
    )

    if matches:
        samples = [float(m) for m in matches[1:-1]] or [float(m) for m in matches]
        packet_per_burst = sum(samples) / len(samples)
    else:
        packet_per_burst = ''
    return packet_per_burst

def _get_all_pkts(dpdk_output) -> int:

    match = re.search(r'measured RX packets:\s*(\d+)', dpdk_output)
    if match:
        rx_packets = int(match.group(1))
        # tqdm.write(f"RX packets: {rx_packets}")
    else:
        rx_packets = -1
        tqdm.write("RX packets not found.")
    
    return rx_packets

def _extract_cores(base_command):
    # cerca "-l 0,1" oppure "-l 0-3"
    match = re.search(r'-l\s+([0-9,\-]+)', base_command)
    if match:
        return match.group(1)
    return "0"  # fallback

def policy_args(policy):
    return f"--policy {policy}"

# Steady-state perf windows, taken after the NDR search on the same process
# while TRex sends at the NDR rate. Events are split so the first window
# (cycles, instructions, L1) is never multiplexed; the second one is, and perf
# scales it. The IMC CAS counters run system-wide alongside the second window.
PERF_WARMUP = 3
PERF_WINDOWS = [
    (8, ['cycles', 'instructions', 'L1-dcache-loads', 'L1-dcache-load-misses',
         'L1-dcache-stores', 'branches', 'branch-misses']),
    (12, ['l2_request.all', 'l2_request.miss', 'mem_load_l3_miss_retired.local_dram',
          'dTLB-loads', 'dTLB-load-misses', 'LLC-loads', 'LLC-load-misses',
          'cache-references', 'cache-misses', 'LLC-stores', 'LLC-store-misses']),
]
IMC_EVENTS = ['uncore_imc/cas_count_read/', 'uncore_imc/cas_count_write/']
PERF_EVENTS = [e for _, events in PERF_WINDOWS for e in events]
# per-packet columns written to the CSV (perf events, DRAM bytes, app counters)
METRICS = ([e.replace('-', '_').replace('.', '_') for e in PERF_EVENTS]
           + ['dram_read_bytes', 'dram_write_bytes', 'perf_tx_pps', 'perf_loss_pct'])


def _parse_perf_csv(text):
    """perf stat -x, lines: value,unit,event,runtime,pct,..."""
    out = {}
    for line in text.splitlines():
        f = line.split(',')
        if len(f) < 3 or not f[0] or f[0].startswith('<'):
            continue
        try:
            value = float(f[0])
        except ValueError:
            continue
        if f[1] == 'MiB':
            value *= 1 << 20
        out[f[2]] = value
    return out


def _port_counters(client):
    st = client.get_stats(ports=PORTS)[0]
    return st['opackets'], st['ipackets']


def measure_steady_state(client, process, base_command, pps, core_mask=None):
    """Per-packet perf events on the running app, with TRex at @pps."""
    cores = _extract_cores(base_command)
    launch_trex(client, pps, core_mask)
    sleep(PERF_WARMUP)
    out_mark = len(process._output)
    metrics = {}
    tx_total = rx_total = 0
    for i, (seconds, events) in enumerate(PERF_WINDOWS):
        cmd = ['sudo', 'perf', 'stat', '-x,', '-C', cores, '-e', ','.join(events), 'sleep', str(seconds)]
        imc = None
        if i == len(PERF_WINDOWS) - 1:
            imc = sp.Popen(['sudo', 'perf', 'stat', '-x,', '-a', '-e', ','.join(IMC_EVENTS),
                            'sleep', str(seconds)], stdout=sp.DEVNULL, stderr=sp.PIPE, text=True)
        tx0, rx0 = _port_counters(client)
        res = sp.run(cmd, capture_output=True, text=True)
        imc_out = imc.communicate()[1] if imc else ''
        tx1, rx1 = _port_counters(client)
        tx, rx = tx1 - tx0, rx1 - rx0
        tx_total += tx
        rx_total += rx
        counts = _parse_perf_csv(res.stderr)
        for event in events:
            if event in counts and tx > 0:
                metrics[event.replace('-', '_').replace('.', '_')] = counts[event] / tx
        if imc:
            imc_counts = _parse_perf_csv(imc_out)
            if tx > 0:
                metrics['dram_read_bytes'] = imc_counts.get(IMC_EVENTS[0], 0) / tx
                metrics['dram_write_bytes'] = imc_counts.get(IMC_EVENTS[1], 0) / tx
    seconds = sum(s for s, _ in PERF_WINDOWS)
    metrics['perf_tx_pps'] = tx_total / seconds
    metrics['perf_loss_pct'] = 100 * (tx_total - rx_total) / tx_total if tx_total else ''
    stop_trex(client)
    window_output = process._output[out_mark:]
    return metrics, _get_empty_poll(window_output), _get_packet_per_burst(window_output)


def launch_program(base_command, queue, policy, additional_args):
    command = f'{base_command} -q {queue} {policy_args(policy)} {additional_args}'

    try:
        tqdm.write("Running command:" + command)
        
        # Crea processo con PIPE per leggere output
        process = sp.Popen(
            shlex.split(command),
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            text=True,
            bufsize=1
        )
        
        # Flag per sincronizzazione
        process._ready_event = threading.Event()
        process._output = ""
        
        # Thread per leggere output silenziosamente
        def read_output():
            for line in process.stdout:
                process._output += line
                if "ready" in line.lower() or "entering main loop" in line.lower():
                    process._ready_event.set()
        
        reader_thread = threading.Thread(target=read_output, daemon=True)
        reader_thread.start()
        
        # Aspetta "ready" con timeout
        if process._ready_event.wait(timeout=15):
            tqdm.write("✅ Programma pronto (ready signal ricevuto)")
            return process
        else:
            tqdm.write("⚠️ Timeout: 'ready' non ricevuto entro 15s")
            return -1

    except Exception as e:
        tqdm.write("Exception occurred while running command:" + str(e))
        return -1

def stop_program(process):
    if process and process != -1:
        process.terminate()   # tenta chiusura "soft"
        try:
            process.wait(timeout=60)
        except sp.TimeoutExpired:
            sp.run(['sudo', 'pkill', '-9', '-f', 'build/(nf-chain|mica)'])
            process.wait()
        tqdm.write("Program terminated.")


def extract_final_pps(bench, policy, queue, repetition):
    """Estrae il final_pps dai risultati del benchmark"""
    try:
        results_json = bench.results.to_json()
        if not results_json:
            tqdm.write(f"⚠️ Nessun risultato disponibile per queue={queue}, policy='{policy}', rep={repetition}")
            return None
        
        # Normalizza i dati da JSON
        if isinstance(results_json, str):
            data = json.loads(results_json)
        else:
            data = results_json
        
        # Estrai i risultati interni
        results_data = data.get('results', {})
        
        # Estrai rx_pps
        rx_pps = results_data.get('rx_pps', None)
        
        if rx_pps < 1:
            return 1
        return round(rx_pps , 2)
    except Exception as e:
        tqdm.write(f"❌ Errore nell'estrazione pps: {e}")
        return None


def export_results_to_csv(bench, policy, queue, repetition, perf_metrics, metric_names, empty_poll=None, packet_per_burst=None, csv_file="benchmark_results.csv"):
    """Esporta i risultati di una iterazione (con metriche perf) in CSV strutturato"""
    try:
        results_json = bench.results.to_json()
        if not results_json:
            tqdm.write(f"⚠️ Nessun risultato disponibile per queue={queue}, policy='{policy}', rep={repetition}")
            return
        
        # Normalizza i dati da JSON
        if isinstance(results_json, str):
            data = json.loads(results_json)
        else:
            data = results_json
        
        # Estrai i risultati interni
        results_data = data.get('results', {})
        
        # Estrai campi principali dai risultati
        ndr_value = results_data.get('ndr_points', [None])[0] if results_data.get('ndr_points') else None
        
        row_data = {
            'policy': policy,
            'queue': queue,
            'repetition': repetition,
            'iterations': results_data.get('total_iterations', ''),
            'ndr_bps': ndr_value or '',
            'rate_tx_bps': results_data.get('rate_tx_bps', ''),
            'rate_rx_bps': results_data.get('rate_rx_bps', ''),
            'tx_pps': results_data.get('tx_pps', ''),
            'rx_pps': results_data.get('rx_pps', ''),
            'drop_rate_percentage': results_data.get('drop_rate_percentage', ''),
            'tx_util': results_data.get('tx_util', ''),
            'cpu_util': results_data.get('cpu_util', ''),
            'bw_per_core': results_data.get('bw_per_core', ''),
            'elapsed_time': results_data.get('Elapsed Time', ''),
            'empty_poll': empty_poll if empty_poll is not None else '',
            'packet_per_burst': packet_per_burst if packet_per_burst is not None else '',
        }
        
        # Aggiungi metriche perf dinamicamente
        for col_name in metric_names:
            row_data[col_name] = perf_metrics.get(col_name, '')
        
        # Scrivi header se il file non esiste
        file_exists = os.path.isfile(csv_file)
        with open(csv_file, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=row_data.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(row_data)
        
        tqdm.write(f"✅ Risultati + perf salvati in {csv_file}")
    except Exception as e:
        tqdm.write(f"❌ Errore nell'esportazione CSV: {e}")


def calculate_and_export_averages(metric_names, csv_file="benchmark_results.csv", averages_csv="benchmark_averages.csv"):
    """Calcola le medie per ogni gruppo di ripetizioni (policy, queue) e le esporta in un CSV separato"""
    try:
        if not os.path.isfile(csv_file):
            tqdm.write(f"⚠️ File {csv_file} non trovato")
            return
        
        # Leggi il CSV dei risultati
        data = []
        with open(csv_file, 'r', newline='') as f:
            reader = csv.DictReader(f)
            data = list(reader)
        
        if not data:
            tqdm.write(f"⚠️ Nessun dato trovato in {csv_file}")
            return
        
        # Raggruppa per policy e queue
        groups = {}
        for row in data:
            policy = row['policy']
            queue = row['queue']
            key = (policy, queue)
            
            if key not in groups:
                groups[key] = []
            groups[key].append(row)
        
        # Calcola le medie e deviazioni standard per ogni gruppo
        averages = []
        numeric_fields = [
            'iterations', 'ndr_bps', 'rate_tx_bps', 'rate_rx_bps', 
            'tx_pps', 'rx_pps', 'drop_rate_percentage', 'tx_util', 
            'cpu_util', 'bw_per_core', 'elapsed_time'
        ]
        
        # Aggiungi i campi perf dinamicamente
        numeric_fields += list(metric_names) + ['empty_poll', 'packet_per_burst']
        
        for (policy, queue), group_rows in sorted(groups.items()):
            avg_row = {'policy': policy, 'queue': queue, 'repetitions_count': len(group_rows)}
            
            # Calcola la media e deviazione standard per ogni campo numerico
            for field in numeric_fields:
                values = []
                for row in group_rows:
                    try:
                        if row.get(field) in (None, ''):
                            continue
                        values.append(float(row[field]))
                    except (ValueError, TypeError):
                        pass
                
                if values:
                    avg_val = sum(values) / len(values)
                    avg_row[f'{field}_avg'] = round(avg_val, 4)
                    
                    # Calcola deviazione standard (usa sample std dev se più di 1 valore)
                    if len(values) > 1:
                        std_dev = statistics.stdev(values)
                    else:
                        std_dev = 0
                    avg_row[f'{field}_stdev'] = round(std_dev, 4)
                else:
                    avg_row[f'{field}_avg'] = ''
                    avg_row[f'{field}_stdev'] = ''
            
            averages.append(avg_row)
        
        # Scrivi il CSV con le medie e deviazioni standard
        if averages:
            fieldnames = ['policy', 'queue', 'repetitions_count']
            for field in numeric_fields:
                fieldnames.append(f'{field}_avg')
                fieldnames.append(f'{field}_stdev')
            
            with open(averages_csv, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(averages)
            
            tqdm.write(f"✅ Medie e deviazioni standard salvate in {averages_csv} | policy='{policy}', queue={queue}")
        else:
            tqdm.write(f"⚠️ Nessuna media da calcolare")
    
    except Exception as e:
        tqdm.write(f"❌ Errore nel calcolo delle medie: {e}")

def launch_trex(client, pps, core_mask=None):
    client.start(ports=PORTS, mult=f"{pps}pps", force=True, core_mask=core_mask)
    # aspetta wamup traffico a pps
    tqdm.write("Waiting for traffic to reach expected rate before stopping warmup...")

    for _ in range(30):
        stats = client.get_stats(ports=[0])
        current_pps = stats[0]['tx_pps']
        if current_pps >= int(pps) * 0.9:  # check if traffic is at least 90% of expected
            # print(f"Traffic is at {current_pps} pps, stopping warmup.")
            tqdm.write(f"Traffic reached {current_pps} pps, stopping warmup.")
            break
        sleep(1)
    return

def stop_trex(client):
    tqdm.write("Stopping TRex traffic...")
    client.stop(ports=PORTS)
    for _ in range(30):
        stats = client.get_stats(ports=PORTS)
        if stats[0]['tx_pps'] < 2:
            break
        sleep(1)

    tqdm.write("Traffic fully stopped.")

def release_trex(client):
    """Leave the TRex port without streams.

    A client that connects syncs every stream already on the port, and with the
    10000 Zipf streams that reply takes the server's RPC thread past its 1 s
    watchdog, which aborts TRex (TrexRpcCmdGetAllStreams in the crash trace).
    """
    try:
        client.stop(ports=PORTS)
        client.reset(ports=PORTS)
    finally:
        client.disconnect()

# above pdr (0.1%) with margin: the binary search never found a lossless rate
NDR_FAILED_DROP_PCT = 0.2


def ndr_failed(row):
    try:
        return float(row['drop_rate_percentage']) > NDR_FAILED_DROP_PCT
    except (KeyError, ValueError):
        return True


def clear_csv_files():
    """Rimuove i file CSV esistenti per risultati e medie"""
    for csv_file in ["benchmark_results.csv", "benchmark_averages.csv"]:
        if os.path.isfile(csv_file):
            os.remove(csv_file)
            tqdm.write(f"🔄 CSV precedente rimosso ({csv_file})")

def set_governor(governor):
    for core in range(9):
        path = f"/sys/devices/system/cpu/cpu{core}/cpufreq/scaling_governor"
        try:
            sp.run(
                ["sudo", "tee", path],
                input=governor,
                text=True,
                stdout=sp.DEVNULL,
                check=True
            )
            print(f"Core {core}: {governor} OK")
        except sp.CalledProcessError:
            print(f"Core {core}: errore")

BOOST = "/sys/devices/system/cpu/cpufreq/boost"


def set_boost(value):
    """Turbo off during the runs (fixed 3.1 GHz as in the paper); returns the old value."""
    try:
        with open(BOOST) as f:
            old = f.read().strip()
    except OSError:
        return None
    sp.run(["sudo", "tee", BOOST], input=str(value), text=True, stdout=sp.DEVNULL, check=False)
    return old

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_NDR = os.path.join(REPO, "experiments", "results", "ndr")  # results_<experiment>_skew<s>/
QDMA_EAL = "-d librte_net_qdma.so -n 4 -a 0000:16:00.1"
NF_CHAIN = (f"sudo {REPO}/nf-chain/build/nf-chain {QDMA_EAL} -l {{lcores}} -- -p 0x3 "
            f"--config=\"(0,0,0),(0,1,1)\" "
            f"--rule_ipv4={REPO}/nf-chain/rules/fw_10k --rule_ipv6={REPO}/nf-chain/rules/rule_ipv6.db")
MICA = f"sudo {REPO}/mica/build/mica {QDMA_EAL} -l {{lcores}} --"
ALL_QUEUES = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048]

# Zero-loss (RFC2544 NDR) experiments of the paper. 'policies' are --policy
# values; skew 0.1 is the near-uniform trace, 0.6 low skew, 0.9 high skew.
EXPERIMENTS = {
    # Fig. 3 and 5: NF chain on one core
    'nf-chain': {
        'base_command': NF_CHAIN.format(lcores="0").replace("(0,0,0),(0,1,1)", "(0,0,0)"),
        'policies': ["baseline", "aggressive", "penalty", "aggressive+penalty"],
        'repetitions': 2,
        'queues': ALL_QUEUES,
        'zipf_skews': [0.1, 0.6, 0.9],
    },
    # Fig. 8: MICA on one core
    'mica': {
        'base_command': MICA.format(lcores="4"),
        'policies': ["baseline", "aggressive", "penalty"],
        'repetitions': 1,
        'queues': ALL_QUEUES,
        'zipf_skews': [0.1, 0.6, 0.9],
    },
}

# Fig. 7, 9 and 10: Shared and Partitioned with 2, 4 and 8 cores
for cores, lcores in ((2, "0,1"), (4, "0-3"), (8, "0-7")):
    queues = [q for q in ALL_QUEUES if q >= cores]
    for multicore in ("shared", "partitioned"):
        EXPERIMENTS[f'nf-chain-{multicore}{cores}'] = {
            'base_command': f"{NF_CHAIN.format(lcores=lcores)} --multicore {multicore}",
            'policies': ["aggressive+penalty"],
            'repetitions': 1,
            'queues': queues,
            'zipf_skews': [0.1, 0.6, 0.9],
        }
        EXPERIMENTS[f'mica-{multicore}{cores}'] = {
            'base_command': f"{MICA.format(lcores=lcores)} --multicore {multicore}",
            'policies': ["aggressive+penalty"],
            'repetitions': 5 if multicore == "shared" else 3,
            'queues': queues,
            'zipf_skews': [0.1, 0.6, 0.9],
        }


# Throughput bound of a perfect queue-selection policy: the same Zipf flows sent
# one after the other (sequential-profile.py), so every burst holds one flow,
# on 1 queue. TRex must send from one core, or it splits the trains over its
# cores and interleaves the flows again.
for app, command in (('nf-chain', EXPERIMENTS['nf-chain']['base_command']),
                     ('mica', EXPERIMENTS['mica']['base_command'])):
    EXPERIMENTS[f'{app}-seq'] = {
        'base_command': command,
        'policies': ["baseline"],
        'repetitions': 3,
        'queues': [1],
        'zipf_skews': [0.1, 0.6, 0.9],
        'profile': "sequential-profile.py",
        'trex_core_mask': [0x1],
    }
    # the same with the largest RX ring the QDMA accepts (8192 descriptors,
    # completion ring 16385): on 1 queue the 1024-packet ring absorbs only
    # ~340 us of stall at 3 Mpps, and the zero-loss rate stops well below
    # what the core can process
    EXPERIMENTS[f'{app}-seq-ring8192'] = dict(EXPERIMENTS[f'{app}-seq'], additional_args="-d 8192")

# Do the packet buffers of many queues push MICA's items out of the LLC? The same
# aggressive points with 256-descriptor rings (a quarter of the buffers)
EXPERIMENTS['mica-ring256'] = dict(EXPERIMENTS['mica'], policies=["aggressive"], queues=[64, 512],
                                   repetitions=2, zipf_skews=[0.6], additional_args="-d 256")


def main():
    parser = argparse.ArgumentParser(description="Zero-loss throughput (RFC2544 NDR) with TRex")
    parser.add_argument("experiments", nargs="*", help="experiments to run: " + ", ".join(EXPERIMENTS))
    parser.add_argument("--resume", action="store_true",
                        help="keep existing CSVs and skip the (policy, queue, repetition) already in them")
    parser.add_argument("--queues", type=int, nargs="+", help="override the queue counts of the experiments")
    parser.add_argument("--policies", nargs="+", help="override the policies of the experiments")
    parser.add_argument("--skews", type=float, nargs="+", help="override the Zipf skews of the experiments")
    parser.add_argument("--repetitions", type=int, help="override the repetitions of the experiments")
    parser.add_argument("--tag", default="", help="suffix of the results folders, results/ndr/results_<experiment><tag>_skew*")
    args = parser.parse_args()
    unknown = [e for e in args.experiments if e not in EXPERIMENTS]
    if not args.experiments or unknown:
        parser.error(f"choose from: {', '.join(EXPERIMENTS)}")
    commands_config = {name: dict(EXPERIMENTS[name]) for name in args.experiments}
    for config in commands_config.values():
        for key, value in (('queues', args.queues), ('policies', args.policies),
                           ('zipf_skews', args.skews), ('repetitions', args.repetitions)):
            if value is not None:
                config[key] = value

    client = STLClient(server="100.78.72.16")
    old_boost = None

    try:
        tqdm.write("Connessione a TRex...")
        client.connect()

        set_governor("performance")
        old_boost = set_boost(0)
        
        # Calcola il numero totale di iterazioni
        total_iterations = sum(
            len(config['policies']) * len(config['queues']) * config['repetitions'] * len(config['zipf_skews'])
            for config in commands_config.values()
        )
        
        # Progress bar globale
        pbar = tqdm(total=total_iterations, desc="Esperimento", unit="test", leave=True)
        
        # Loop sui comandi e skew values
        for cmd_name, cmd_config in commands_config.items():
            base_command = cmd_config['base_command']
            policies = cmd_config['policies']
            queues = cmd_config['queues']
            repetitions = cmd_config['repetitions']
            zipf_skews = cmd_config['zipf_skews']
            additional_args = cmd_config.get('additional_args', '')
            profile_file = cmd_config.get('profile', PROFILE_FILE)
            core_mask = cmd_config.get('trex_core_mask')

            for zipf_skew in zipf_skews:
                tqdm.write(f"\n{'='*80}")
                tqdm.write(f"TEST: {cmd_name} | Zipf Skew: {zipf_skew}")
                tqdm.write(f"{'='*80}\n")
                
                # Crea cartella per i risultati
                results_dir = os.path.join(RESULTS_NDR, f"results_{cmd_name}{args.tag}_skew{zipf_skew}")
                os.makedirs(results_dir, exist_ok=True)
                
                # Percorsi CSV nella cartella di risultati
                csv_file = os.path.join(results_dir, "benchmark_results.csv")
                averages_csv = os.path.join(results_dir, "benchmark_averages.csv")
                
                # Azzera il CSV se esiste, o con --resume salta i test già fatti
                done = set()
                if os.path.isfile(csv_file):
                    if args.resume:
                        # le ricerche NDR fallite (app che non inoltra) si ripetono
                        with open(csv_file, newline='') as f:
                            reader = csv.DictReader(f)
                            fields, rows = reader.fieldnames, list(reader)
                        good = [r for r in rows if not ndr_failed(r)]
                        if len(good) < len(rows):
                            with open(csv_file, 'w', newline='') as f:
                                writer = csv.DictWriter(f, fieldnames=fields)
                                writer.writeheader()
                                writer.writerows(good)
                        done = {(r['policy'], int(r['queue']), int(r['repetition'])) for r in good}
                    else:
                        os.remove(csv_file)
                
                tqdm.write(f"Reset porta 0...")

                client.reset(ports=PORTS)
                
                tqdm.write(f"Carico stream da profilo Zipf {zipf_skew} Python...")
                streams = STLProfile.load_py(
                    profile_file,
                    direction=0,
                    port_id=0,
                    num_flows=10000,
                    # num_flows=1,
                    skew=zipf_skew,
                    packet_size=64,
                ).get_streams()
                
                tqdm.write(f"✅ Caricati {len(streams):,} stream dal profilo Zipf\n")
                client.add_streams(streams, ports=PORTS)
                
                # CONFIG NDR
                config = NdrBenchConfig(
                    ports=PORTS,
                    cores=1,
                    iteration_duration=20.0,
                    first_run_duration=20.0,
                    max_iterations=100,
                    pdr=0.1, #0.2 0.09 
                    pdr_error=0.1, 
                    bi_dir=False,
                    verbose=False,
                    opt_binary_search=True,
                    opt_binary_search_percentage=0.1,
                    plugin_file="ndr_plugin_stats.py",
                )
                config.receive_ports = [0]
                if core_mask is not None:
                    config.transmit_core_masks = core_mask
                
                tqdm.write(f"🚀 Avvio benchmark NDR (zero packet loss)...\n")

                for policy in policies:
                    for queue in queues:
                        for repetition in range(repetitions):
                            pbar.set_description(f"[{cmd_name:12} |skew:{zipf_skew:.2f}| Q:{queue:4d} | {policy:12} | Rep:{repetition+1}/{repetitions}]")
                            if (policy, queue, repetition + 1) in done:
                                pbar.update(1)
                                continue

                            process = launch_program(base_command, queue, policy, additional_args)
                            if process == -1:
                                tqdm.write(f"❌ {cmd_name} skew={zipf_skew} Q={queue} {policy}: app non partita, salto")
                                sp.run(['sudo', 'pkill', '-9', '-f', 'build/(nf-chain|mica)'])
                                sleep(5)
                                pbar.update(1)
                                continue
                            try:
                                bench = NdrBench(client, config)
                                bench.find_ndr()
                                final_pps = extract_final_pps(bench, policy, queue, repetition+1)
                                if final_pps and process.poll() is None:
                                    # metriche perf sullo stesso processo, a regime al rate NDR
                                    perf_metrics, empty_poll, packet_per_burst = measure_steady_state(
                                        client, process, base_command, final_pps, core_mask)
                                    export_results_to_csv(bench, policy, queue, repetition+1, perf_metrics, METRICS,
                                                          empty_poll, packet_per_burst, csv_file)
                                else:
                                    tqdm.write(f"❌ {cmd_name} skew={zipf_skew} Q={queue} {policy}: "
                                               f"NDR non trovato o app terminata")
                            except Exception as e:
                                tqdm.write(f"❌ {cmd_name} skew={zipf_skew} Q={queue} {policy}: {e!r}")
                                try:
                                    stop_trex(client)
                                except Exception:
                                    pass
                            finally:
                                stop_program(process)

                            pbar.update(1)
                        # Calcola e salva la media per questo gruppo di ripetizioni
                        calculate_and_export_averages(METRICS, csv_file, averages_csv)

                tqdm.write(f"\n✅ Risultati salvati in {results_dir}/")
        
        pbar.close()
    
    finally:
        set_governor("schedutil")
        if old_boost is not None:
            set_boost(old_boost)
        tqdm.write("\nDisconnessione...")
        release_trex(client)


if __name__ == "__main__":
    main()
