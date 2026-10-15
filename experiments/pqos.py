#!/usr/bin/env python3
"""
Lancia sudo pqos -m "all:4" -i 10 -u csv e stampa ogni secondo
media e deviazione standard su una finestra scorrevole di 5 secondi.
"""

import subprocess
import collections
import statistics
from datetime import datetime

WINDOW = 10  # campioni (1 campione = 1 secondo con -i 10)


def detect_header(line):
    """Ritorna True se la riga sembra un'intestazione CSV pqos valida."""
    parts = [p.strip() for p in line.split(',')]
    # Serve almeno 2 colonne separate da virgola
    if len(parts) < 2:
        return False
    # Almeno un campo non numerico (il nome della colonna)
    for p in parts:
        cleaned = p.replace('.', '').replace('-', '').replace(':', '').replace(' ', '')
        if cleaned and not cleaned.isdigit():
            return True
    return False


def main():
    cmd = ["sudo", "pqos", "-m", "all:4", "-i", "10", "-u", "csv"]
    print(f"Lancio: {' '.join(cmd)}")
    print(f"Finestra media: {WINDOW} secondi\n")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    headers: list[str] = []
    time_field: str = ""
    numeric_fields: list[str] = []
    header_found = False

    current_time = None
    current_batch = collections.defaultdict(list)  # metric -> [valori dei core]

    # windows[metric] = deque delle medie per timestamp (max WINDOW elementi)
    windows = collections.defaultdict(lambda: collections.deque(maxlen=WINDOW))

    header_printed = False

    def flush_batch(_ts, batch):
        nonlocal header_printed
        for metric, values in batch.items():
            if values:
                windows[metric].append(sum(values) / len(values))

        active = [(m, list(dq)) for m, dq in windows.items() if dq]
        if not active:
            return

        if not header_printed:
            cols = ",".join(f"{m}_mean,{m}_std" for m, _ in active)
            print(f"timestamp,{cols}")
            header_printed = True

        now = datetime.now().strftime("%H:%M:%S")
        vals_csv = ",".join(
            f"{sum(v)/len(v):.3f},{statistics.stdev(v) if len(v)>1 else 0.0:.3f}"
            for _, v in active
        )
        print(f"{now},{vals_csv}")

    assert proc.stdout is not None
    try:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue

            # Salta righe di testo/avviso senza virgole (non sono CSV)
            if ',' not in line:
                continue

            # Rilevamento header
            if not header_found:
                if detect_header(line):
                    headers = [h.strip() for h in line.split(',')]
                    # Identifica il campo tempo
                    time_field = next(
                        (h for h in headers if any(k in h.lower() for k in ('time', 'date'))),
                        headers[0],
                    )
                    numeric_fields = [h for h in headers if h != time_field]
                    header_found = True
                    print(f"Header rilevato: {headers}")
                    print(f"Campo tempo: {time_field}")
                    print(f"Metriche: {numeric_fields}\n")
                continue

            # Parsing riga dati
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < len(headers):
                continue
            row = dict(zip(headers, parts))

            ts = row.get(time_field, '')

            # Nuovo timestamp -> svuota il batch precedente
            if ts != current_time:
                if current_batch:
                    flush_batch(current_time, dict(current_batch))
                current_time = ts
                current_batch = collections.defaultdict(list)

            # Accumula valori numerici del timestamp corrente (più core)
            for field in numeric_fields:
                val_str = row.get(field, '')
                try:
                    current_batch[field].append(float(val_str))
                except ValueError:
                    pass

    except KeyboardInterrupt:
        print("\nInterrotto.")
        if current_batch:
            flush_batch(current_time, dict(current_batch))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
