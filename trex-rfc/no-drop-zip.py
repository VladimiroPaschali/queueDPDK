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

    print("Test veloce loopback (sanity check)...")
    client.start(ports=PORTS, mult="10pps", duration=5)
    client.wait_on_traffic(ports=PORTS)
    stats = client.get_stats()[0]
    tx = stats["opackets"]
    rx = stats["ipackets"]
    print(f"TX={tx} RX={rx}")
    if rx == 0:
        raise RuntimeError("❌ Nessun traffico di ritorno! Loopback NON funzionante")

    # if abs(tx - rx) > 10:
    #     raise RuntimeError(f"❌ Differenza TX/RX troppo alta: {tx-rx}")

    print("✅ Loopback OK\n")

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

def launch_program_with_perf(base_command, queue, policy, perf_events):
    perf_events_str = ','.join(perf_events)
    perf_prefix = f'sudo perf stat -C 4 -e {perf_events_str} --timeout 20000'
    command = f'{perf_prefix} {base_command} -q {queue} {policy}'

    try:
        result = sp.run(shlex.split(command), capture_output=True, text=True, check=True)

    except sp.CalledProcessError as e:
        print("Error running perf command:", e)
        return {}
    
    perf_data = _parse_perf_output(result.stderr)
    return perf_data



def launch_program(base_command, queue, policy):
    command = f'{base_command} -q {queue} {policy}'

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
        
        # Thread per leggere output silenziosamente
        def read_output():
            for line in process.stdout:
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
        print("Exception occurred while running command:", str(e))
        return -1

def stop_program(process):
    if process:
        process.terminate()   # tenta chiusura "soft"
        process.wait()        
        tqdm.write("Program terminated.")


def extract_final_pps(bench, policy, queue, repetition):
    """Estrae il final_pps dai risultati del benchmark"""
    try:
        results_json = bench.results.to_json()
        if not results_json:
            print(f"⚠️ Nessun risultato disponibile per queue={queue}, policy='{policy}', rep={repetition}")
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
        
        return rx_pps
    except Exception as e:
        print(f"❌ Errore nell'estrazione pps: {e}")
        return None


def export_results_to_csv(bench, policy, queue, repetition, perf_metrics, perf_events, csv_file="benchmark_results.csv"):
    """Esporta i risultati di una iterazione (con metriche perf) in CSV strutturato"""
    try:
        results_json = bench.results.to_json()
        if not results_json:
            print(f"⚠️ Nessun risultato disponibile per queue={queue}, policy='{policy}', rep={repetition}")
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
        print(f"❌ Errore nell'esportazione CSV: {e}")


def calculate_and_export_averages(perf_events, csv_file="benchmark_results.csv", averages_csv="benchmark_averages.csv"):
    """Calcola le medie per ogni gruppo di ripetizioni (policy, queue) e le esporta in un CSV separato"""
    try:
        if not os.path.isfile(csv_file):
            print(f"⚠️ File {csv_file} non trovato")
            return
        
        # Leggi il CSV dei risultati
        data = []
        with open(csv_file, 'r', newline='') as f:
            reader = csv.DictReader(f)
            data = list(reader)
        
        if not data:
            print(f"⚠️ Nessun dato trovato in {csv_file}")
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
            
            print(f"✅ Medie e deviazioni standard salvate in {averages_csv} | policy='{policy}', queue={queue}")
        else:
            print(f"⚠️ Nessuna media da calcolare")
    
    except Exception as e:
        print(f"❌ Errore nel calcolo delle medie: {e}")
    
def launch_trex(client, pps):
    client.start(ports=PORTS, mult=f"{pps}pps", force=True)
    # aspetta wamup traffico a pps
    print("Waiting for traffic to reach expected rate before stopping warmup...")

    while True:
        stats = client.get_stats(ports=[0])
        current_pps = stats[0]['tx_pps']
        if current_pps >= int(pps) * 0.9:  # check if traffic is at least 90% of expected
            # print(f"Traffic is at {current_pps} pps, stopping warmup.")
            print(f"Traffic reached {current_pps} pps, stopping warmup.")
            break
        sleep(1)
    return

def stop_trex(client):
    print("Stopping TRex traffic...")
    client.stop(ports=PORTS)
    while True:
        stats = client.get_stats(ports=PORTS)
        if stats[0]['tx_pps'] < 2:
            break
        sleep(1)

    print("Traffic fully stopped.")

def clear_csv_files():
    """Rimuove i file CSV esistenti per risultati e medie"""
    for csv_file in ["benchmark_results.csv", "benchmark_averages.csv"]:
        if os.path.isfile(csv_file):
            os.remove(csv_file)
            print(f"🔄 CSV precedente rimosso ({csv_file})")

def main():
    client = STLClient(server="10.181.120.102")
    repetitions = 3
    queues = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048]
    
    # Definisci i comandi e le loro configurazioni
    commands_config = {
        'chain': {
            'policies': ["", "-a", "-a -s 5", "-s 20", "-a -s 20", "-s 50"],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat/build/asni-nat -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,4)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db'
        },
        'marco_chain': {
            'policies': ["", "-a"],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/marco/dpdk-20.11/queueDPDK/asni-nat/build/asni-nat -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1,indirect_queues=1024 -- -p 0x3 --config="(0,0,4)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db'
        },
        'mica': {
            'policies': ["", "-a", "-a -s 5", "-s 20", "-a -s 20", "-s 50"],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/queueDPDK/toasty-mica/build/toasty-mica -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- '
        },
        'marco_mica': {
            'policies': ["", "-a"],
            'base_command': 'sudo /home/vladimiro/dpdk_patched/marco/dpdk-20.11/queueDPDK/toasty-mica/build/toasty-mica -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- '
        }
    }
    
    # Zipf skew values
    zipf_skews = [0.6, 0.9]
    
    # Eventi perf da raccogliere
    perf_events = [
        'cycles', 'instructions', 'cache-references', 'cache-misses',
        'L1-dcache-loads', 'L1-dcache-load-misses', 'L1-dcache-stores',
        'l2_request.miss', 'l2_request.all', 'dTLB-loads', 'dTLB-load-misses',
        'LLC-loads', 'LLC-load-misses', 'LLC-stores', 'LLC-stores-misses',
        'mem_load_l3_miss_retired.local_dram'
    ]
    
    try:
        print("Connessione a TRex...")
        client.connect()
        
        # Calcola il numero totale di iterazioni
        total_iterations = sum(
            len(config['policies']) * len(queues) * repetitions
            for config in commands_config.values()
        ) * len(zipf_skews)
        
        # Progress bar globale
        pbar = tqdm(total=total_iterations, desc="Esperimento", unit="test", leave=True)
        
        # Loop sui comandi e skew values
        for cmd_name, cmd_config in commands_config.items():
            for zipf_skew in zipf_skews:
                print(f"\n{'='*80}")
                print(f"TEST: {cmd_name} | Zipf Skew: {zipf_skew}")
                print(f"{'='*80}\n")
                
                # Crea cartella per i risultati
                results_dir = f"results_{cmd_name}_skew{zipf_skew}"
                os.makedirs(results_dir, exist_ok=True)
                
                # Percorsi CSV nella cartella di risultati
                csv_file = os.path.join(results_dir, "benchmark_results.csv")
                averages_csv = os.path.join(results_dir, "benchmark_averages.csv")
                
                # Azzera il CSV se esiste
                if os.path.isfile(csv_file):
                    os.remove(csv_file)
                
                print(f"Reset porta 0...")
                client.reset(ports=PORTS)
                
                print(f"Carico stream da profilo Zipf {zipf_skew} Python...")
                streams = STLProfile.load_py(
                    PROFILE_FILE,
                    direction=0,
                    port_id=0,
                    num_flows=10000,
                    skew=zipf_skew,
                    packet_size=64
                ).get_streams()
                
                print(f"✅ Caricati {len(streams):,} stream dal profilo Zipf\n")
                client.add_streams(streams, ports=PORTS)
                
                # CONFIG NDR
                config = NdrBenchConfig(
                    ports=PORTS,
                    cores=1,
                    iteration_duration=10.0,
                    first_run_duration=10.0,
                    max_iterations=100,
                    pdr=0.1,          
                    pdr_error=1.0,
                    bi_dir=False,
                    verbose=False,
                    opt_binary_search=True,
                    opt_binary_search_percentage=0.5,
                    plugin_file="ndr_plugin_stats.py",
                )
                config.receive_ports = [0]
                
                print(f"🚀 Avvio benchmark NDR (zero packet loss)...\n")
                
                base_command = cmd_config['base_command']
                policies = cmd_config['policies']
                
                for policy in policies:
                    for queue in queues:                
                        for repetition in range(repetitions):
                            pbar.set_description(f"[{cmd_name:12} | Q:{queue:4d} | {policy:12} | Rep:{repetition+1}/{repetitions}]")
                            
                            bench = NdrBench(client, config)
                            process = launch_program(base_command, queue, policy)
                            bench.find_ndr()
                            stop_program(process)
                            
                            # Estrai final_pps dai risultati del benchmark
                            final_pps = extract_final_pps(bench, policy, queue, repetition+1)
                            
                            if final_pps:
                                # Esegui perf per ottenere le metriche
                                perf_metrics = launch_program_with_perf(base_command, queue, policy, perf_events)
                                # Esporta risultati benchmark + metriche perf in CSV
                                export_results_to_csv(bench, policy, queue, repetition+1, perf_metrics, perf_events, csv_file)
            
                            pbar.update(1)
                        # Calcola e salva la media per questo gruppo di ripetizioni
                        calculate_and_export_averages(perf_events, csv_file, averages_csv)
                            
                
                print(f"\n✅ Risultati salvati in {results_dir}/")
        
        pbar.close()
    
    finally:
        print("\nDisconnessione...")
        client.disconnect()


if __name__ == "__main__":
    main()