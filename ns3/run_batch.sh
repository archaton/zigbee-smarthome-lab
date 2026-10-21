#!/usr/bin/env bash
# Uruchamia N niezależnych przebiegów scenariusza (kolejne wartości --run) i scala wyniki w jeden CSV.
#
# Dlaczego wiele krótkich przebiegów zamiast jednego długiego: w ns-3 3.44 wpisy tras w routerach
# wygasają ok. 128 s po utworzeniu i po tym czasie opóźnienia na ścieżkach przez routery są zawyżone
# (scenariusz odmawia pracy z oknem dłuższym niż --maxWindow). Statystykę zbieramy więc z wielu
# krótkich, niezależnych przebiegów.
#
# Użycie:  ./run_batch.sh [N=20] [wynik.csv=ns3_results.csv] [dodatkowe opcje scenariusza...]
# Przykład: ./run_batch.sh 30 wyniki.csv --topology=topology.csv --fading=1.5
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${BIN:-$HERE/zigbee-profile-gen}"
N="${1:-20}"
OUT="${2:-ns3_results.csv}"
shift $(( $# >= 2 ? 2 : $# )) || true
EXTRA=("$@")

if [[ ! -x "$BIN" ]]; then
  echo "Brak $BIN. Najpierw uruchom ./build.sh" >&2
  exit 1
fi
if ! [[ "$N" =~ ^[0-9]+$ ]] || (( N < 1 )); then
  echo "N musi być dodatnią liczbą całkowitą" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --topology domyślnie wskazuje topology.csv obok skryptu
HAS_TOPO=0
for a in "${EXTRA[@]:-}"; do [[ "$a" == --topology=* ]] && HAS_TOPO=1; done
(( HAS_TOPO )) || EXTRA+=("--topology=$HERE/topology.csv")

for (( r = 1; r <= N; r++ )); do
  if ! "$BIN" "${EXTRA[@]}" --run="$r" --out="$TMP/run_$r.csv" > "$TMP/run_$r.log" 2>&1; then
    echo "Przebieg $r nie powiódł się:" >&2
    tail -n 5 "$TMP/run_$r.log" >&2
    exit 1
  fi
  printf "\rprzebieg %d/%d" "$r" "$N" >&2
done
echo >&2

# scalanie: komentarz z pierwszego przebiegu, jeden nagłówek, dane ze wszystkich
{
  echo "# merged_runs=$N; parametry pierwszego przebiegu poniżej"
  grep -m1 '^#' "$TMP/run_1.csv" || true
  grep -m1 -v '^#' "$TMP/run_1.csv"
  for (( r = 1; r <= N; r++ )); do
    grep -v '^#' "$TMP/run_$r.csv" | tail -n +2
  done
} > "$OUT"

echo "Zapisano $OUT ($(grep -vc '^#' "$OUT") wierszy z $N przebiegów)"
