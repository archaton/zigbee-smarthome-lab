#!/usr/bin/env bash
# Kompiluje zigbee-profile-gen.cc względem kół pip `ns3` (bez budowania ns-3 ze źródeł).
#
# Wymagania: g++ >= 11 (C++20), Python 3 z zainstalowanym pakietem:  pip install ns3==3.44
# Opcjonalnie wskaż instalację ręcznie:  NS3_DIR=/ścieżka/do/site-packages/ns3 ./build.sh
#
# Uwaga: skrypt był testowany z ns3==3.44 (moduł Zigbee z samą warstwą NWK).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

if [[ -z "${NS3_DIR:-}" ]]; then
  # nie importujemy samego ns3 (ciężka inicjalizacja cppyy), tylko szukamy katalogu w site-packages
  NS3_DIR="$("$PY" -c 'import sysconfig, os; print(os.path.join(sysconfig.get_paths()["purelib"], "ns3"))')"
fi

if [[ ! -d "$NS3_DIR/include/ns3" || ! -d "$NS3_DIR/lib64" ]]; then
  echo "Nie znaleziono ns-3 w: $NS3_DIR" >&2
  echo "Zainstaluj:  $PY -m pip install ns3==3.44   albo ustaw NS3_DIR." >&2
  exit 1
fi

# sufiks wersji z nazwy biblioteki, np. libns3.44-core.so -> 44 (ns-3 3.44)
CORE_LIB="$(ls "$NS3_DIR"/lib64/libns3.*-core.so 2>/dev/null | head -n1 || true)"
if [[ -z "$CORE_LIB" ]]; then
  echo "Brak libns3.*-core.so w $NS3_DIR/lib64" >&2
  exit 1
fi
VER="$(basename "$CORE_LIB" | sed -E 's/^libns3\.([0-9.]+)-core\.so$/\1/')"
if [[ ! -e "$NS3_DIR/lib64/libns3.$VER-zigbee.so" ]]; then
  echo "Ta instalacja ns-3 ($VER) nie zawiera modułu zigbee." >&2
  exit 1
fi

OUT="${1:-$HERE/zigbee-profile-gen}"
LIBS=()
for m in core network mobility propagation spectrum lr-wpan zigbee; do
  LIBS+=("-lns3.$VER-$m")
done

echo "ns-3 (biblioteki libns3.$VER-*) z $NS3_DIR"
g++ -std=c++20 -O2 -Wall \
  -I"$NS3_DIR/include" \
  "$HERE/zigbee-profile-gen.cc" \
  -L"$NS3_DIR/lib64" -Wl,-rpath,"$NS3_DIR/lib64" \
  "${LIBS[@]}" \
  -o "$OUT"
echo "Zbudowano: $OUT"
