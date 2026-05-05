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
        last_10 = [float(m) for m in matches[-10:-2]]
        empty_poll = sum(last_10) / len(last_10)
        # print("Empty poll (ultimi 10):", last_10)
        # print("Empty poll (media):", empty_poll)
    else:
        empty_poll = -1
        print("Empty poll not found")

    return empty_poll

def _get_packet_per_burst(dpdk_output) -> int:
    matches = re.findall(
        # r'packets/burst:\s*(\d+)',
        r'pkts/burst.*?:\s*([\d.]+)',
        dpdk_output,
        re.IGNORECASE
    )

    if matches:
        last_10 = [float(m) for m in matches[-10:-2]]
        packet_per_burst = sum(last_10) / len(last_10)
        print("Packets per burst (ultimi 10):", last_10)
        print("Packets per burst (media):", packet_per_burst)
    else:
        packet_per_burst = -1
        print("Packets per burst not found")    
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

def launch_program_with_perf(base_command, queue, policy, perf_events, final_pps):
    perf_events_str = ','.join(perf_events)
    perf_timeout = 20  

    cores = _extract_cores(base_command)

    perf_prefix = f'sudo perf stat -C {cores} -e {perf_events_str} --timeout {perf_timeout * 1000}'
    command = f'{perf_prefix} {base_command} -q {queue} {policy}'

    try:
        result = sp.run(shlex.split(command), capture_output=True, text=True, check=True)
    except sp.CalledProcessError as e:
        tqdm.write("Error running perf command:" + str(e))
        return {}
    
    perf_data = _parse_perf_output(result.stderr)
    all_pkts = _get_all_pkts(result.stdout)
    throughput = _get_dpdk_throughput(result.stdout)

    if all_pkts <= 0:
        tqdm.write("⚠️ All packets count normalizing with last trex throughput.")
        perf_data = {k: v / final_pps * perf_timeout for k, v in perf_data.items()}
    else:
        perf_data = {k: v / all_pkts for k, v in perf_data.items()}

    return perf_data



def launch_program(base_command, queue, policy, additional_args):
    command = f'{base_command} -q {queue} {policy} {additional_args}'

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
        process.wait()
        
        empty_poll = _get_empty_poll(process._output)
        packet_per_burst = _get_packet_per_burst(process._output)
        
        tqdm.write("Program terminated.")
        return empty_poll, packet_per_burst
    return None, None


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


def export_results_to_csv(bench, policy, queue, repetition, perf_metrics, perf_events, empty_poll=None, packet_per_burst=None, csv_file="benchmark_results.csv"):
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
            'policy': policy if policy else 'default',
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
        for event in perf_events:
            col_name = event.replace('-', '_').replace('.', '_')
            row_data[col_name] = perf_metrics.get(event, '')
        
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


def calculate_and_export_averages(perf_events, csv_file="benchmark_results.csv", averages_csv="benchmark_averages.csv"):
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
        for event in perf_events:
            numeric_fields.append(event.replace('-', '_').replace('.', '_'))
        
        for (policy, queue), group_rows in sorted(groups.items()):
            avg_row = {'policy': policy, 'queue': queue, 'repetitions_count': len(group_rows)}
            
            # Calcola la media e deviazione standard per ogni campo numerico
            for field in numeric_fields:
                values = []
                for row in group_rows:
                    try:
                        val = float(row.get(field, 0)) if row.get(field) else 0
                        values.append(val)
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

def launch_trex(client, pps):
    client.start(ports=PORTS, mult=f"{pps}pps", force=True)
    # aspetta wamup traffico a pps
    tqdm.write("Waiting for traffic to reach expected rate before stopping warmup...")

    while True:
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
    while True:
        stats = client.get_stats(ports=PORTS)
        if stats[0]['tx_pps'] < 2:
            break
        sleep(1)

    tqdm.write("Traffic fully stopped.")

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

def main():
    client = STLClient(server="10.181.120.102")

    # Definisci i comandi e le loro configurazioni
    commands_config = {
        'chain': {
            # 'policies': ["","-a","-s 5", "-a -s 5"],
            'policies': [""],
            'repetitions': 1,
            'queues': [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048],
            # 'queues': [1, 512],
            'zipf_skews': [0.6,0.9],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat/build/asni-nat -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,4)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db',
        },
        # 'chain2048d': {
        #     'policies': ["-a -s 5"],
        #     'repetitions': 10,
        #     'queues': [1, 2, 64, 256, 512, 1024],
        #     'zipf_skews': [0.6],
        #     'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat/build/asni-nat -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,4)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db',
        #     'additional_args': '-d 2048'
        # },
        # 'chain4096d': {
        #     'policies': ["-a -s 5"],
        #     'repetitions': 10,
        #     'queues': [1, 2, 64, 256, 512, 1024],
        #     'zipf_skews': [0.6],
        #     'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat/build/asni-nat -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,4)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db',
        #     'additional_args': '-d 4096'
        # },
        # '2chain8192d': {
        #     'policies': ["-a -s 5"],
        #     'repetitions': 10,
        #     # 'queues': [1, 2, 64, 256, 512],
        #     'queues': [1, 512],
        #     'zipf_skews': [0.6],
        #     'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat/build/asni-nat -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,4)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db',
        #     'additional_args': '-d 8192'
        # },
        # 'marco_chain': {
        #     'policies': ["", "-a"],
        #     'base_command': 'sudo /home/vladimiro/dpdk_patched/marco/dpdk-20.11/queueDPDK/asni-nat/build/asni-nat -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1,indirect_queues=1024 -- -p 0x3 --config="(0,0,4)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db'
        # },
        # 'marco_mica': {
        #     'policies': ["", "-a"],
        #     'base_command': 'sudo /home/vladimiro/dpdk_patched/marco/dpdk-20.11/queueDPDK/toasty-mica/build/toasty-mica -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1,indirect_queues=1024 -- '
        # },
        'mica': {
            'policies': [ "", "-a", "-s 5", "-a -s 5" ],
            'repetitions': 1,
            # 'queues': [1, 512],
            'queues': [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048],
            'zipf_skews': [0, 0.9],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/toasty-mica/build/toasty-mica -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- '
        },
        'chain-multicore2': {
            'policies': ["-a -s 5"],
            'repetitions': 1,
            'queues': [2, 4, 8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
            'zipf_skews': [0.6, 0.9],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat-multicore/build/asni-nat -d librte_net_qdma.so -l 0,1 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,0),(0,1,1)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db'
        },
        'chain-multicore4': {
            'policies': ["-a -s 5"],
            'repetitions': 1,
            'queues': [4, 8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
            'zipf_skews': [0.6, 0.9],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat-multicore/build/asni-nat -d librte_net_qdma.so -l 0-3 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,0),(0,1,1)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db'
        },
        'chain-multicore8': {
            'policies': ["-a -s 5"],
            'repetitions': 1,
            'queues': [8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
            'zipf_skews': [0.6, 0.9],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat-multicore/build/asni-nat -d librte_net_qdma.so -l 0-7 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,0),(0,1,1)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db'
        },
        # 'chain-multicore-corec2': {
        #     'policies': ["-a -s 5"],
        #     'repetitions': 1,
        #     'queues': [2, 4, 8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
        #     'zipf_skews': [0.6, 0.9],
        #     'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat-multicore-corec/build/asni-nat -d librte_net_qdma.so -l 0,1 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,0),(0,1,1)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db'
        # },
        # 'chain-multicore-corec4': {
        #     'policies': ["-a -s 5"],
        #     'repetitions': 1,
        #     'queues': [4, 8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
        #     'zipf_skews': [0.6, 0.9],
        #     'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat-multicore-corec/build/asni-nat -d librte_net_qdma.so -l 0-3 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,0),(0,1,1)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db'
        # },
        # 'chain-multicore-corec8': {
        #     'policies': ["-a -s 5"],
        #     'repetitions': 1,
        #     'queues': [8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
        #     'zipf_skews': [0.6, 0.9],
        #     'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat-multicore-corec/build/asni-nat -d librte_net_qdma.so -l 0-7 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,0),(0,1,1)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db'
        # },
        'mica-multicore2': {
            'policies': ["-a -s 5"],
            'repetitions': 1,
            'queues': [2, 4, 8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
            'zipf_skews': [0, 0.9],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/toasty-mica-multicore/build/toasty-mica -d librte_net_qdma.so -l 0,1 -n 4 -a 0000:16:00.1 -- '

        },
        'mica-multicore4': {
            'policies': ["-a -s 5"],
            'repetitions': 1,
            'queues': [4, 8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
            'zipf_skews': [0, 0.9],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/toasty-mica-multicore/build/toasty-mica -d librte_net_qdma.so -l 0-3 -n 4 -a 0000:16:00.1 -- '
        },
        'mica-multicore8': {
            'policies': ["-a -s 5"],
            'repetitions': 1,
            'queues': [8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
            'zipf_skews': [0, 0.9],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/toasty-mica-multicore/build/toasty-mica -d librte_net_qdma.so -l 0-7 -n 4 -a 0000:16:00.1 -- '
        },
        # 'mica-multicore-corec2': {
        #     'policies': ["-a -s 5"],
        #     'repetitions': 1,
        #     'queues': [2, 4, 8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
        #     'zipf_skews': [0.6, 0.9],
        #     'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/toasty-mica-multicore-corec/build/toasty-mica -d librte_net_qdma.so -l 0,1 -n 4 -a 0000:16:00.1 -- '

        # },
        # 'mica-multicore-corec4': {
        #     'policies': ["-a -s 5"],
        #     'repetitions': 1,
        #     'queues': [4, 8, 16, 32, 64, 128, 256, 512 ,1024, 2048],
        #     'zipf_skews': [0.6, 0.9],
        #     'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/toasty-mica-multicore-corec/build/toasty-mica -d librte_net_qdma.so -l 0-3 -n 4 -a 0000:16:00.1 -- '
        # },

    }
    
    
    # Eventi perf da raccogliere
    perf_events = [
        'cycles', 'instructions', 'cache-references', 'cache-misses',
        'L1-dcache-loads', 'L1-dcache-load-misses', 'L1-dcache-stores',
        'l2_request.miss', 'l2_request.all', 'dTLB-loads', 'dTLB-load-misses',
        'LLC-loads', 'LLC-load-misses', 'LLC-stores', 'LLC-stores-misses',
        'mem_load_l3_miss_retired.local_dram'
    ]
    
    try:
        tqdm.write("Connessione a TRex...")
        client.connect()

        set_governor("performance")
        
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

            for zipf_skew in zipf_skews:
                tqdm.write(f"\n{'='*80}")
                tqdm.write(f"TEST: {cmd_name} | Zipf Skew: {zipf_skew}")
                tqdm.write(f"{'='*80}\n")
                
                # Crea cartella per i risultati
                results_dir = f"results_{cmd_name}_skew{zipf_skew}"
                os.makedirs(results_dir, exist_ok=True)
                
                # Percorsi CSV nella cartella di risultati
                csv_file = os.path.join(results_dir, "benchmark_results.csv")
                averages_csv = os.path.join(results_dir, "benchmark_averages.csv")
                
                # Azzera il CSV se esiste
                if os.path.isfile(csv_file):
                    os.remove(csv_file)
                
                tqdm.write(f"Reset porta 0...")

                client.reset(ports=PORTS)
                
                tqdm.write(f"Carico stream da profilo Zipf {zipf_skew} Python...")
                streams = STLProfile.load_py(
                    PROFILE_FILE,
                    direction=0,
                    port_id=0,
                    num_flows=10000,
                    # num_flows=1,
                    skew=zipf_skew,
                    packet_size=64
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
                
                tqdm.write(f"🚀 Avvio benchmark NDR (zero packet loss)...\n")

                for policy in policies:
                    for queue in queues:                
                        for repetition in range(repetitions):
                            policy_desc = policy if policy else "default"
                            pbar.set_description(f"[{cmd_name:12} |skew:{zipf_skew:.2f}| Q:{queue:4d} | {policy_desc:12} | Rep:{repetition+1}/{repetitions}]")
                            
                            bench = NdrBench(client, config)
                            process = launch_program(base_command, queue, policy, additional_args)
                            bench.find_ndr()
                            empty_poll, packet_per_burst = stop_program(process)
                            
                            # Estrai final_pps dai risultati del benchmark
                            final_pps = extract_final_pps(bench, policy, queue, repetition+1)
                            
                            if final_pps:
                                # Esegui perf per ottenere le metriche
                                launch_trex(client, final_pps)
                                perf_metrics = launch_program_with_perf(base_command, queue, policy, perf_events, final_pps)
                                stop_trex(client)
                                # Esporta risultati benchmark + metriche perf in CSV
                                export_results_to_csv(bench, policy, queue, repetition+1, perf_metrics, perf_events, empty_poll, packet_per_burst, csv_file)
            
                            pbar.update(1)
                        # Calcola e salva la media per questo gruppo di ripetizioni
                        calculate_and_export_averages(perf_events, csv_file, averages_csv)
                            
                
                tqdm.write(f"\n✅ Risultati salvati in {results_dir}/")
        
        pbar.close()
    
    finally:
        set_governor("schedutil")
        tqdm.write("\nDisconnessione...")
        client.disconnect()


if __name__ == "__main__":
    main()
