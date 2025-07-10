import csv
import json
import os
import re
import shlex
import signal
import subprocess as sp
import threading
import numpy as np
from time import sleep
from hooks import before_exp, after_exp
import psutil
from tqdm import tqdm
import time


def _append_to_log(log_path: str, data: str) -> int:
    with open(log_path, "a") as f:
        f.write(data)
    return 0

def _clear_file(log_path: str) -> int:
    with open(log_path, "w") as f:
        pass
    return 0

def _init_csv(csv_path: str, cfg_throughput: bool, cfg_perf: list) -> int:
    fields = []

    fields.append("repetition")
    fields.append("program")
    fields.append("queues")
    fields.append("descriptors")
    fields.append("cms")
    fields.append("aggressive")
    if cfg_throughput:
        fields.append("throughput")
    fields.extend(cfg_perf)
    fields.append("max_bw")
    fields.append("avg_bw")

    # Create the CSV file and write the header
    with open(csv_path, "w",newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(fields)


def _append_to_csv(csv_path: str, data: list) -> int:
    with open(csv_path, "a") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(data)


def _get_dpdk_throughput(dpdk_output) -> int:

    match = re.search(r'RX packets:\s*(\d+)', dpdk_output)
    if match:
        rx_packets = int(match.group(1))
        print("RX packets:", rx_packets)
    else:
        rx_packets = -1
        print("RX packets not found.")
    
    return rx_packets

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

def parse_bw(output: str) -> float:
    mbl_values = []
    lines = output.strip().splitlines()
    for line in lines:
        line = line.strip()
        if line.startswith("CORE") or line.startswith("TIME") or not line:
            continue  # salta intestazioni e righe non rilevanti
        parts = line.split()
        if len(parts) > 0:
            try:
                mbl = float(parts[3])
                mbl_values.append(mbl)
            except ValueError:
                continue
    if not mbl_values:
        raise ValueError("Nessun valore MBL trovato.")
    
    avg_mbl = sum(mbl_values) / len(mbl_values)
    max_mbl = max(mbl_values)
    return max_mbl, avg_mbl

def _get_bw(cfg_cpu, cfg_time, result_bw) -> int:

    command = f"sudo pqos -m 'mbl:{cfg_cpu}' -t {int(cfg_time/2)}"
    # print("Running bandwidth command:", command)
    try:
        result = sp.run(shlex.split(command), capture_output=True, text=True, check=True)
    except sp.CalledProcessError as e:
        print("Error running pqos command:", e)
        return -1
    max_bw, avg_bw = parse_bw(result.stdout)

    result_bw["max_bw"] = max_bw
    result_bw["avg_bw"] = avg_bw
    return 0

def _load_with_perf(cfg_cpu, cfg_perf, cfg_time, program_path, cfg_dpdk_args ,cfg_ifpci, cfg_app_args, cfg_queues, cfg_aggressive, cfg_descriptors, cfg_cms, cfg_per_pkt) -> int:

    #sudo perf stat -C 4 -e cycles,instructions,cache-references,cache-misses,L1-dcache-loads,L1-dcache-load-misses,L1-dcache-stores,
    # l2_request.miss,l2_request.all,dTLB-loads,dTLB-load-misses,LLC-loads,LLC-load-misses,LLC-stores
    #   --timeout 2000  ./build/cms -d librte_net_qdma.so -l 4-10 -n 4 -a 16:00.1 16:00.0  -- -p 1 -T 1 -q 1

    aggressive = "-a" if cfg_aggressive else ""
    perf_timeout = cfg_time * 1000  # Convert to milliseconds
    perf_events = ",".join(cfg_perf)

    command = f"sudo perf stat -C {cfg_cpu} -e {perf_events} --timeout {perf_timeout} {program_path} {cfg_dpdk_args} -a {cfg_ifpci}"
    command += f" -- {cfg_app_args} -q {cfg_queues} -d {cfg_descriptors} {aggressive} -c {cfg_cms} -p 1 "
    # print(command)

    #lancia un thread diverso per calcolare la banda
    result_bw = {"max_bw": 0, "avg_bw": 0}
    thread = threading.Thread(target=_get_bw, args=(cfg_cpu, cfg_time, result_bw))
    thread.start()

    try:
        result = sp.run(shlex.split(command),capture_output=True,text=True, check=True)

    except sp.CalledProcessError as e:
        print("Error running perf command:", e)
        return -1
    
    thread.join()  # Attende il thread che calcola la banda

    max_bw = result_bw["max_bw"]
    avg_bw = result_bw["avg_bw"]
    
    
    # print(result.stderr)
    # print(result.stdout)
    perf_data = _parse_perf_output(result.stderr)
    # print(perf_data)
    throughput = _get_dpdk_throughput(result.stdout)
    if throughput <= 0:
        print("Error: Throughput is zero or negative")
        return -1

    if cfg_per_pkt:
        perf_data_per_pkt = {k: v/ throughput for k, v in perf_data.items()}
        perf_data = perf_data_per_pkt

    perf_data["max_bw"] = max_bw
    perf_data["avg_bw"] = avg_bw
    # print("Perf data:", perf_data)

    return perf_data, throughput

def run_suite(suite_cfg:json, name:str) -> int:

    logpath = os.path.join(os.getcwd(),"suites", name, "log.txt")
    csvpath = os.path.join(os.getcwd(),"suites", name, "results.csv")
    allcsvpath = os.path.join(os.getcwd(),"suites", name, "all_results.csv")
    cfg_absolute_path = os.path.abspath(suite_cfg["exp-dir"])
    cfg_ifpci = suite_cfg["ifpci"]
    cfg_time = suite_cfg["time"]
    cfg_cpu = suite_cfg["cpu"]
    cfg_repetitions = suite_cfg["repetitions"]
    cfg_queues = suite_cfg["queues"]
    cfg_descriptors = suite_cfg["descriptors"]
    cfg_throughput = suite_cfg["throughput"]
    cfg_perf = suite_cfg["perf"]
    cfg_per_pkt = suite_cfg["per-pkt"]
    cfg_programs = suite_cfg["progs"]
    cfg_cms = suite_cfg["cms"]
    #if aggressive is 2 it means both aggressive and non-aggressive
    cfg_aggressive = {
        0: [False],
        1: [True],
        2: [True, False]
    }.get(suite_cfg["aggressive"], [False])
    #clear csv and sets up fiels
    _init_csv(csvpath, cfg_throughput, cfg_perf)
    _init_csv(allcsvpath, cfg_throughput, cfg_perf)
    print(f"CSV file initialized at {csvpath}")

    #clear logfile
    _clear_file(logpath)

    csvdata =[]
    allcsvdata = []


    cfg_program_path=os.path.join(cfg_absolute_path, cfg_programs[0]["path"])
    cfg_dpdk_args = cfg_programs[0]["dpdk-args"]
    cfg_app_args = cfg_programs[0]["app-args"]


    tot_iterations = cfg_repetitions * len(cfg_queues) * len(cfg_descriptors) * len(cfg_cms) * len(cfg_aggressive)
    tot_time = cfg_time * tot_iterations

    print(f"Total estimated time for the experiment: {tot_time} seconds (~{tot_time/60:.2f} minutes)")

    # Barra di progresso con tqdm
    with tqdm(total=tot_iterations, desc="Running experiments", unit="run") as pbar:
        for queue in cfg_queues:
            for descriptor in cfg_descriptors:
                for cms in cfg_cms:
                    for aggressive in cfg_aggressive:

                        csvdata.append(1)  # Repetition
                        csvdata.append(cfg_programs[0]["name"])
                        csvdata.append(queue)
                        csvdata.append(descriptor)
                        csvdata.append(cms)
                        csvdata.append(aggressive)

                        print(f"Running program: {cfg_programs[0]['name']} with queues={queue}, descriptors={descriptor}, cms columns={cms}, aggressive={aggressive}")

                        start_time = time.time()

                        perf_data, throughput = _load_with_perf(
                            cfg_cpu, cfg_perf, cfg_time, cfg_program_path,
                            cfg_dpdk_args, cfg_ifpci, cfg_app_args,
                            queue, aggressive, descriptor, cms, cfg_per_pkt
                        )

                        csvdata.append(throughput)
                        csvdata.extend(perf_data.values())
                        _append_to_csv(csvpath, csvdata)
                        csvdata.clear()

                        pbar.update(1)
                        elapsed = time.time() - start_time
                        pbar.set_postfix({"Last run (s)": f"{elapsed:.1f}", "Throughput": f"{throughput:.1f}"})


    return 0