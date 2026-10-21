#!/usr/bin/env python3
"""Konwertuje wyniki scenariusza ns-3 (zigbee-profile-gen) do profilu sieci symulatora.

Dla każdego urządzenia z profilu liczy z sond w górę (urządzenie -> koordynator), bez sond
rozgrzewkowych:
  latency_ms         średnie opóźnienie jednokierunkowe [ms]
  latency_jitter_ms  odchylenie standardowe opóźnienia [ms]
  loss               odsetek sond utraconych lub spóźnionych (status lost/late)
oraz dopisuje informacyjny blok "_ns3" (percentyle, przedział ufności utraty, opóźnienie pierwszej
sondy z wykrywaniem trasy, wyniki w dół dla routerów). Symulator ignoruje nieznane klucze.

Uwaga: rozkład opóźnień jest prawostronnie skośny (retransmisje MAC), a symulator losuje opóźnienie
z rozkładu normalnego (średnia, odchylenie), więc ogon rozkładu jest uproszczony. Pełne dane są
w CSV.

Użycie:
  python3 ns3_to_profile.py WYNIKI.csv ../sim/network_profile.json [-o WYJSCIE.json]
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys
from collections import defaultdict

REQUIRED = {"mode", "run", "direction", "device", "hops", "seq", "warmup", "tx_s", "rx_s",
            "latency_ms", "status"}


def read_results(path):
    comments, lines = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            (comments if line.startswith("#") else lines).append(line)
    reader = csv.DictReader(lines)
    missing = REQUIRED - set(reader.fieldnames or [])
    if missing:
        sys.exit(f"BŁĄD: w {path} brakuje kolumn: {', '.join(sorted(missing))} "
                 "(wyniki z innej wersji scenariusza?)")
    return comments, list(reader)


def wilson(k, n, z=1.96):
    """Przedział ufności Wilsona 95% dla odsetka k/n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1))))]


def summarize(rows):
    n = len(rows)
    ok = sorted(float(r["latency_ms"]) for r in rows if r["status"] == "ok")
    lost = n - len(ok)
    lo, hi = wilson(lost, n)
    return {
        "n": n,
        "ok": len(ok),
        "loss": lost / n if n else None,
        "loss_ci95": (lo, hi),
        "mean": statistics.fmean(ok) if ok else None,
        "std": statistics.stdev(ok) if len(ok) > 1 else 0.0,
        "p50": percentile(ok, 0.50),
        "p95": percentile(ok, 0.95),
        "max": ok[-1] if ok else None,
    }


def ols(xs, ys):
    n = len(xs)
    if n < 2 or len(set(xs)) < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    icpt = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (icpt + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return slope, icpt, r2


def fmt(v, spec=".1f"):
    return "-" if v is None else format(v, spec)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", help="CSV ze scenariusza ns-3 (także scalony przez run_batch.sh)")
    ap.add_argument("profile", help="istniejący network_profile.json (nazwy urządzeń i hopy)")
    ap.add_argument("-o", "--output", help="plik wyjściowy (domyślnie <profil>.ns3.json obok profilu)")
    ap.add_argument("--update-hops", action="store_true",
                    help="przyjmij liczbę hopów z ns-3, gdy różni się od profilu (domyślnie błąd)")
    ap.add_argument("--update-globals", action="store_true",
                    help="zaktualizuj też globalne latency_ms_per_hop i loss_per_hop (przybliżenie)")
    ap.add_argument("--min-samples", type=int, default=200,
                    help="ostrzegaj, gdy urządzenie ma mniej mierzonych sond (domyślnie 200)")
    args = ap.parse_args()

    comments, rows = read_results(args.results)
    with open(args.profile, encoding="utf-8") as f:
        profile = json.load(f)

    up = defaultdict(list)
    warm_up = defaultdict(list)
    down = defaultdict(list)
    hops = {}
    runs = set()
    for r in rows:
        dev = r["device"]
        hops[dev] = int(r["hops"])
        runs.add(r["run"])
        if r["direction"] == "up":
            (warm_up if r["warmup"] == "1" else up)[dev].append(r)
        elif r["warmup"] == "0":
            down[dev].append(r)

    # kontrola zgodności topologii z profilem
    problems, missing = [], []
    for cfg in profile["devices"]:
        name = cfg["name"]
        if name not in hops:
            missing.append(name)
        elif hops[name] != int(cfg.get("hops", 1)) and not args.update_hops:
            problems.append(f"{name}: profil ma {cfg.get('hops', 1)} hopów, ns-3 {hops[name]}")
    if problems:
        sys.exit("BŁĄD: topologia ns-3 różni się od profilu (użyj --update-hops, jeśli to zamierzone):\n  "
                 + "\n  ".join(problems))
    for name in missing:
        print(f"OSTRZEŻENIE: urządzenia {name} nie ma w wynikach ns-3, zostawiam wartości z profilu.",
              file=sys.stderr)

    print(f"Przebiegi: {len(runs)}, wierszy: {len(rows)}\n")
    header = (f"{'urządzenie':<24}{'hopy':>5}{'sond':>7}{'utrata %':>10}{'(95% CI)':>16}"
              f"{'śr. ms':>8}{'odch.':>7}{'p95':>7}{'1. sonda':>10}")
    print(header)
    fit_x, fit_y, jit_per_hop, loss_per_hop = [], [], [], []
    for cfg in profile["devices"]:
        name = cfg["name"]
        if name not in hops:
            continue
        s = summarize(up[name])
        first = [float(r["latency_ms"]) for r in warm_up[name] if r["status"] == "ok"]
        first_ms = statistics.fmean(first) if first else None
        h = hops[name]
        ci = s["loss_ci95"]
        print(f"{name:<24}{h:>5}{s['n']:>7}{fmt(100 * s['loss'] if s['loss'] is not None else None):>10}"
              f"{'(' + fmt(100 * ci[0]) + '-' + fmt(100 * ci[1]) + ')':>16}"
              f"{fmt(s['mean']):>8}{fmt(s['std']):>7}{fmt(s['p95']):>7}{fmt(first_ms):>10}")
        if s["n"] < args.min_samples:
            print(f"  OSTRZEŻENIE: {name} ma tylko {s['n']} sond, wynik niepewny "
                  f"(zbierz więcej przebiegów: run_batch.sh).", file=sys.stderr)
        if s["ok"] == 0:
            print(f"OSTRZEŻENIE: {name}: brak udanych sond, nie zmieniam opóźnienia.", file=sys.stderr)
            cfg["loss"] = 1.0
            continue

        cfg["hops"] = h
        cfg["latency_ms"] = round(s["mean"], 2)
        cfg["latency_jitter_ms"] = round(s["std"], 2)
        cfg["loss"] = round(s["loss"], 4)
        info = {
            "samples": s["n"],
            "latency_p50_ms": round(s["p50"], 2),
            "latency_p95_ms": round(s["p95"], 2),
            "latency_max_ms": round(s["max"], 2),
            "loss_ci95": [round(ci[0], 4), round(ci[1], 4)],
        }
        if first_ms is not None:
            info["first_packet_ms"] = round(first_ms, 2)  # z wykrywaniem trasy (AODV)
        if down[name]:
            d = summarize(down[name])
            if d["ok"]:
                info["down"] = {"samples": d["n"], "loss": round(d["loss"], 4),
                                "latency_ms": round(d["mean"], 2),
                                "latency_jitter_ms": round(d["std"], 2)}
        cfg["_ns3"] = info

        fit_x.append(h)
        fit_y.append(s["mean"])
        jit_per_hop.append(s["std"] / math.sqrt(h))
        loss_per_hop.append(1 - (1 - s["loss"]) ** (1.0 / h))

    fit = ols(fit_x, fit_y)
    if fit:
        print(f"\nRegresja średniego opóźnienia po liczbie hopów: {fit[0]:.2f} ms/hop "
              f"+ {fit[1]:.2f} ms (R²={fit[2]:.2f}, {len(fit_x)} urządzeń)")

    if args.update_globals and fit and fit_x:
        profile["latency_ms_per_hop"] = {"mean": round(fit[0], 2),
                                         "jitter": round(statistics.fmean(jit_per_hop), 2)}
        profile["loss_per_hop"] = round(statistics.fmean(loss_per_hop), 4)
        print("Zaktualizowano globalne latency_ms_per_hop i loss_per_hop (przybliżenie).")

    profile["_ns3"] = {
        "source": os.path.basename(args.results),
        "runs": len(runs),
        "rows": len(rows),
        "scenario_header": [c.strip().lstrip("# ").strip() for c in comments[:2]],
        "note": "Wartości latency_ms / latency_jitter_ms / loss pochodzą z symulacji ns-3 "
                "(uplink, bez sond rozgrzewkowych) przy ZAŁOŻONYCH tłumieniach łączy z topology.csv.",
    }

    out = args.output or os.path.splitext(args.profile)[0] + ".ns3.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"\nZapisano {out}")


if __name__ == "__main__":
    main()
