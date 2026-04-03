import subprocess as sp
import shlex
import sys
import time
import csv
import json
import os
import threading
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

def launch_program(queue, policy):
    command = f'sudo /home/vladimiro/dpdk_patched/queueDPDK/asni-nat/build/asni-nat -d librte_net_qdma.so -l 4 -n 4 -a 0000:16:00.1 -- -p 0x3 --config="(0,0,4)" --rule_ipv4=../chain/fw_10k --rule_ipv6=../chain/rule_ipv6.db -q {queue} {policy}'

    try:
        print("Running command:", command)
        
        
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
                if "ready" in line.lower():
                    process._ready_event.set()
        
        reader_thread = threading.Thread(target=read_output, daemon=True)
        reader_thread.start()
        
        # Aspetta "ready" con timeout
        if process._ready_event.wait(timeout=15):
            print("✅ Programma pronto (ready signal ricevuto)")
            return process
        else:
            print("⚠️ Timeout: 'ready' non ricevuto entro 15s")
            return -1

    except Exception as e:
        print("Exception occurred while running command:", str(e))
        return -1
        return -1

def stop_program(process):
    if process:
        process.terminate()   # tenta chiusura "soft"
        process.wait()        
        print("Program terminated.")


def export_results_to_csv(bench, policy, queue, repetition, csv_file="benchmark_results.csv"):
    """Esporta i risultati di una iterazione in CSV strutturato"""
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
        
        # Scrivi header se il file non esiste
        file_exists = os.path.isfile(csv_file)
        with open(csv_file, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=row_data.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(row_data)
        
        # print(f"✅ Risultati salvati in {csv_file}")
        print(f" policy='{policy}', queue={queue}, rep={repetition}| RX: {row_data['rx_pps']} bps | Drop%: {row_data['drop_rate_percentage']}% | time {row_data['elapsed_time']}s")
    except Exception as e:
        print(f"❌ Errore nell'esportazione CSV: {e}")
    


def main(zipf_skew=0.6):
    client = STLClient(server="10.181.120.102")
    repetitions = 5
    queues = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048]
    policies = ["", "-a", "-a -s 5", "-s 20"]
    
    # Azzera il CSV all'inizio
    csv_file = "benchmark_results.csv"
    if os.path.isfile(csv_file):
        os.remove(csv_file)
        print(f"🔄 CSV precedente rimosso ({csv_file})\n")

    try:
        print("Connessione a TRex...")
        client.connect()

        print("Reset porta 0...")
        client.reset(ports=PORTS)

        print(f"Carico stream da profilo Zipf {zipf_skew} Python...")
        
        # Usa il profilo zip_profile.py (generatore Zipf nativo)
        streams = STLProfile.load_py(
            PROFILE_FILE,
            direction=0,
            port_id=0,
            num_flows=10000,   # numero flussi Zipf (max 20000)
            skew=zipf_skew,    # parametro Zipf (come zipfpicap.py)
            packet_size=64     # dimensione pacchetto
        ).get_streams()
        
        print(f"✅ Caricati {len(streams):,} stream dal profilo Zipf\n")

        client.add_streams(streams, ports=PORTS)

        # =========================
        # CONFIG NDR
        # =========================
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
            plugin_file="ndr_plugin_stats.py",
        )
        config.receive_ports = [0]



        bench = NdrBench(client, config)

        print("🚀 Avvio benchmark NDR (zero packet loss)...\n")

        t0 = time.time()

        for policy in policies:
            for queue in queues:
                for repetition in range(repetitions):
                    print(f"\n=== Policy: {policy} | Repetition: {repetition+1}/{repetitions} ===")
                    process = launch_program(queue,policy)
                    bench.find_ndr()
                    stop_program(process)
                    export_results_to_csv(bench, policy, queue, repetition+1)

        print(f"\nTempo totale: {time.time() - t0:.2f}s")

        print("\n=== RISULTATO FINALE ===")
        bench.results.print_final()


    finally:
        print("\nDisconnessione...")
        client.disconnect()


if __name__ == "__main__":
    main()