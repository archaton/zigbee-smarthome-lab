#!/usr/bin/env bash
# Kalibracja łącza: jak średnie tłumienie jednego łącza przekłada się na utratę i opóźnienie.
# Pomaga dobrać wartości "link,...,<dB>" w topology.csv do jakości łączy znanej z pomiarów.
#
# Użycie:  ./calibrate_link.sh [m_zaniku=1.5] [próbki=600]
#   m_zaniku: parametr Nakagami (0 = bez zaniku: łącza prawie binarne, 1 = Rayleigh)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${BIN:-$HERE/zigbee-profile-gen}"
FADING="${1:-1.5}"
PROBES="${2:-600}"
[[ -x "$BIN" ]] || { echo "Brak $BIN. Najpierw uruchom ./build.sh" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for L in 90 95 100 102 104 106 107 108 109 110; do
  printf "node,coordinator,coordinator\nnode,dev,enddevice\nlink,coordinator,dev,%s\n" "$L" > "$TMP/t_$L.csv"
  # --maxWindow=0: pojedyncze łącze nie ma tras, więc limit okna nie jest potrzebny
  "$BIN" --topology="$TMP/t_$L.csv" --out="$TMP/o_$L.csv" --probes="$PROBES" --slot=0.1 \
         --warmup=1 --maxWindow=0 --fading="$FADING" > "$TMP/log_$L.txt" 2>&1 || true
done

python3 - "$TMP" "$FADING" "$PROBES" <<'EOF'
import csv, os, statistics, sys
tmp, fading, probes = sys.argv[1], sys.argv[2], sys.argv[3]
print(f"Kalibracja pojedynczego łącza (Nakagami m={fading}, {probes} sond/punkt)")
print(f"{'tłum. dB':>9}{'utrata %':>10}{'śr. ms':>8}{'p50':>7}{'p95':>8}{'maks':>8}")
for L in (90, 95, 100, 102, 104, 106, 107, 108, 109, 110):
    path = os.path.join(tmp, f"o_{L}.csv")
    if not os.path.exists(path):
        print(f"{L:>9}   węzeł nie dołączył (łącze poza użytecznym zakresem)")
        continue
    with open(path, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(l for l in f if not l.startswith("#")) if r["warmup"] == "0"]
    ok = sorted(float(r["latency_ms"]) for r in rows if r["status"] == "ok")
    n = len(rows)
    q = lambda p: ok[min(len(ok) - 1, int(p * (len(ok) - 1)))] if ok else float("nan")
    mean = statistics.mean(ok) if ok else float("nan")
    print(f"{L:>9}{100 * (n - len(ok)) / n:>10.1f}{mean:>8.1f}{q(.5):>7.1f}{q(.95):>8.1f}{(max(ok) if ok else float('nan')):>8.1f}")
EOF
