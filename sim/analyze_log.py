#!/usr/bin/env python3
"""Podsumowanie logu symulatora: ile zdarzeń, odsetek zgubionych, opóźnienie (średnia, p95) per urządzenie.

Trzy tabele:
  - raporty w górę (direction=up): wysłane, zgubione, opóźnienie,
  - polecenia w dół (/set, /get): wysłane, zgubione, opóźnienie. Polecenia do czujników
    (command_ignored) nie wchodzą do statystyk,
  - zdarzenia po stronie bramki (direction=bridge): stan optymistyczny, zmiany availability.

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


class Stats:
    def __init__(self):
        self.sent = defaultdict(int)
        self.dropped = defaultdict(int)
        self.delays = defaultdict(list)

    def add(self, name, row):
        self.sent[name] += 1
        if row["dropped"] == "1":
            self.dropped[name] += 1
        else:
            self.delays[name].append(float(row["delay_ms"]))


def main(path):
    up, down = Stats(), Stats()
    hops = {}
    bridge_events = defaultdict(lambda: defaultdict(int))
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row["device"]
            direction = row["direction"]
            if direction == "up":
                hops[name] = int(row["hops"])
                up.add(name, row)
            elif direction == "down" and row["event"] in ("command", "get"):
                hops.setdefault(name, int(row["hops"]))
                down.add(name, row)
            elif direction == "bridge":
                bridge_events[name][row["event"]] += 1

    def table(title, stats, with_p95):
        header = f"{title:<24}{'hopy':>5}{'wysłane':>9}{'zgubione %':>12}{'śr. ms':>9}"
        if with_p95:
            header += f"{'p95 ms':>9}"
        print(header)
        for name in sorted(stats.sent, key=lambda n: (hops[n], n)):
            d = stats.delays[name]
            mean = f"{statistics.mean(d):.1f}" if d else "-"
            loss = 100.0 * stats.dropped[name] / stats.sent[name]
            line = f"{name:<24}{hops[name]:>5}{stats.sent[name]:>9}{loss:>12.1f}{mean:>9}"
            if with_p95:
                line += f"{(f'{percentile(d, 0.95):.1f}' if d else '-'):>9}"
            print(line)

    table("urządzenie", up, True)
    if down.sent:
        print()
        table("polecenia w dół", down, False)
    if bridge_events:
        print("\nzdarzenia po stronie bramki:")
        for name in sorted(bridge_events):
            counts = ", ".join(f"{k}={v}" for k, v in sorted(bridge_events[name].items()))
            print(f"  {name:<22}{counts}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
