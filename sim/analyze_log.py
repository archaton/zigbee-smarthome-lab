#!/usr/bin/env python3
"""Podsumowanie logu symulatora: ile zdarzeń, odsetek zgubionych, opóźnienie (średnia, p95) per urządzenie.

Użycie: python3 sim/analyze_log.py sim/logs/sim_log.csv
"""
import csv
import statistics
import sys
from collections import defaultdict


def percentile(values, q):
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def main(path):
    delays = defaultdict(list)
    sent = defaultdict(int)
    dropped = defaultdict(int)
    hops = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["direction"] != "up":
                continue
            name = row["device"]
            hops[name] = int(row["hops"])
            sent[name] += 1
            if row["dropped"] == "1":
                dropped[name] += 1
            else:
                delays[name].append(float(row["delay_ms"]))

    print(f"{'urządzenie':<24}{'hopy':>5}{'wysłane':>9}{'zgubione %':>12}{'śr. ms':>9}{'p95 ms':>9}")
    for name in sorted(sent, key=lambda n: (hops[n], n)):
        d = delays[name]
        mean = f"{statistics.mean(d):.1f}" if d else "-"
        p95 = f"{percentile(d, 0.95):.1f}" if d else "-"
        loss = 100.0 * dropped[name] / sent[name]
        print(f"{name:<24}{hops[name]:>5}{sent[name]:>9}{loss:>12.1f}{mean:>9}{p95:>9}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
