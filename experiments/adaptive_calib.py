"""Poll mix (empty / full polls, busy) and loss of the fixed queue counts at several rates,
to calibrate the thresholds of --adaptive. Usage: python3 adaptive_calib.py OUT.csv"""
import csv, importlib.util, os, re, sys, time
from time import sleep
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("adp", os.path.join(HERE, "adaptive.py"))
adp = importlib.util.module_from_spec(spec); spec.loader.exec_module(adp)
from trex.stl.api import STLClient

RE_MIX = re.compile(r"poll mix: active polls (\d+), empty ([\d.]+)%, full ([\d.]+)%, busy ([\d.]+)"
                    r"(?:, backlog max ([\d.]+)%, over half ([\d.]+)%)?")
OUT = sys.argv[1]
QUEUES = [1, 4, 16, 64, 128, 512, 2048]
POLICIES = ["baseline", "aggressive", "penalty"]
RATES = [1.0, 2.0, 2.5, 3.0, 3.3, 3.6]
STEP = 6.0

client = STLClient(server=adp.TREX_SERVER); client.connect(); client.acquire(ports=adp.PORTS, force=True)
client.reset(ports=adp.PORTS)
adp.ndz.set_governor("performance")
traffic = adp.Traffic(client)
rows = []
try:
    for pol in POLICIES:
        for q in QUEUES:
            proc = adp.launch_app(f"-q {q} --policy {pol}")
            try:
                sleep(2)
                for rate in RATES:
                    traffic.apply(adp.phase(rate))
                    sleep(2)
                    st0 = client.get_stats(ports=adp.PORTS)[0]; t0 = time.monotonic(); mark = len(proc.lines)
                    sleep(STEP)
                    st1 = client.get_stats(ports=adp.PORTS)[0]
                    tx = st1["opackets"] - st0["opackets"]; rx = st1["ipackets"] - st0["ipackets"]
                    mix = [RE_MIX.search(l) for _, l in proc.lines[mark:]]
                    mix = [m for m in mix if m][1:]  # skip the first, partly before the window
                    if not mix:
                        continue
                    avg = lambda i: sum(float(m.group(i)) for m in mix) / len(mix)
                    row = dict(policy=pol, queues=q, rate=rate, tx_mpps=tx / STEP / 1e6,
                               loss_pct=100 * (tx - rx) / tx if tx else 0, polls_per_s=avg(1),
                               empty_pct=avg(2), full_pct=avg(3), busy=avg(4),
                               backlog_max_pct=max(float(m.group(5) or 0) for m in mix),
                               backlog_half_pct=sum(float(m.group(6) or 0) for m in mix) / len(mix))
                    rows.append(row); print(row, flush=True)
                traffic.stop()
            finally:
                traffic.stop(); adp.stop_app(proc)
finally:
    adp.ndz.release_trex(client); adp.ndz.set_governor("schedutil")
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
