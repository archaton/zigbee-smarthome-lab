# Scenariusz ns-3: wartości do `network_profile.json`

`zigbee-profile-gen.cc` odtwarza topologię z `sim/network_profile.json` (te same nazwy urządzeń i liczby
hopów) w ns-3 i mierzy dla każdego urządzenia opóźnienie jednokierunkowe, jitter i utratę pakietów.
`ns3_to_profile.py` zamienia wyniki na `latency_ms`, `latency_jitter_ms` i `loss` w profilu symulatora.

## Pliki

| Plik | Rola |
|---|---|
| `topology.csv` | węzły (koordynator/router/urządzenie końcowe) i tłumienia łączy w dB |
| `zigbee-profile-gen.cc` | scenariusz (ns-3 3.44: lr-wpan + warstwa NWK modułu zigbee) |
| `build.sh` | kompilacja przeciw bibliotekom z pakietu pip `ns3` |
| `run_batch.sh` | N niezależnych przebiegów scalonych w jeden CSV |
| `calibrate_link.sh` | zależność utraty i opóźnienia od tłumienia pojedynczego łącza |
| `ns3_to_profile.py` | wyniki -> profil sieci symulatora |

## Użycie

```bash
python3 -m venv venv && . venv/bin/activate && pip install ns3==3.44
./build.sh                                   # tworzy ./zigbee-profile-gen (potrzebny g++ z C++20)
./zigbee-profile-gen --verbose               # pojedynczy przebieg, podsumowanie na ekranie
./run_batch.sh 40 wyniki.csv                 # zalecane: 40 przebiegów (ok. 1200 sond na urządzenie)
python3 ns3_to_profile.py wyniki.csv ../sim/network_profile.json -o ../sim/network_profile.json
```

Tryby: `--mode=isolated` (domyślny; sondy pojedynczo, mierzy opóźnienie łącza) i `--mode=loaded`
(równoległe przepływy Poissona, uwzględnia rywalizację o kanał; użyj z `--probes=20`, bo inaczej
okno pomiarowe przekroczy `--maxWindow`). Ten sam `--seed` i `--run` dają identyczny CSV.

Konwerter domyślnie przerywa, gdy liczba hopów w ns-3 różni się od profilu. `--update-globals`
dopasowuje też globalne `latency_ms_per_hop` i `loss_per_hop` (regresja, przybliżenie).
Przy nadpisywaniu profilu zrób kopię: konwerter zastępuje ręcznie ustawione wartości (np. celowo
zawodną `garage_plug` z `loss: 0.05`) wynikami symulacji.

## Wyniki przykładowe (40 przebiegów, założone tłumienia z `topology.csv`)

| urządzenie | hopy | opóźnienie [ms] | odch. | utrata |
|---|---|---|---|---|
| living_room_climate, kitchen_motion, tv_plug | 1 | 2,8-2,9 | 0,7-0,8 | 0% |
| bedroom_climate, front_door_contact | 2 | 6,5-6,6 | 1,1-1,2 | 0% |
| hallway_motion, garage_plug | 3 | 10,4-10,9 | 1,8-2,3 | 0% |
| balcony_door_contact | 4 | 16,3 | 3,7 | 1,2% |

Regresja: ok. 4,3 ms na hop. Pierwsza sonda do urządzeń za 3 hopami trwa 110-160 ms (wykrywanie
trasy), co zapisuje blok `_ns3.first_packet_ms`.

## Ograniczenia (ważne przy cytowaniu w artykule)

- **Tłumienia łączy są założone, nie zmierzone.** Wyniki są tak wiarygodne jak te liczby. Skalibruj je
  z RSSI/LQI (mapa sieci w Zigbee2MQTT); `calibrate_link.sh` pokazuje, jak dB przekłada się na utratę.
- **Moduł Zigbee w ns-3 3.44 to tylko warstwa NWK**, bez APS, ZCL, bezpieczeństwa i walidacji względem
  prawdziwych urządzeń. Opóźnienia (ok. 3 ms/hop) są niższe niż w typowych sieciach sprzętowych
  (dziesiątki ms), bo model nie uwzględnia czasu przetwarzania w układach ani pollingu.
- **Urządzenia końcowe mierzone są tylko w górę** (czujniki bateryjne); model nie ma pollingu rodzica.
- Wszystkie węzły jako routery dawały w 3.44 pętlę routingu, dlatego czujniki są urządzeniami końcowymi.
- Wpisy tras wygasają po ok. 128 s bez odświeżania. Stąd krótkie okna pomiarowe i wiele przebiegów
  (`--maxWindow`).
- Statyczny kanał daje łącza prawie binarne, więc dodano zanik Nakagami (`--fading`, domyślnie m=1,5),
  włączany po dołączeniu węzłów.
- Status `NLDE-DATA.confirm` jest w 3.44 zawsze 0xC1, więc sukces oceniamy po dotarciu sondy.
- Duplikaty (retransmisje MAC po zgubionym ACK) są liczone raz: bierze się pierwsze dostarczenie.
- Rozkład opóźnień jest skośny, a symulator losuje z rozkładu normalnego (średnia, odchylenie).
  Percentyle są w bloku `_ns3`, a pełne dane w CSV.
- Licencja: ns-3 to GPLv2; ten scenariusz linkuje się z bibliotekami ns-3, więc jego dystrybucja
  podlega GPLv2. Wyniki (CSV, profil) nie są objęte licencją.
