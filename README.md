# Laboratorium Zigbee / smart home (symulacja + metryki)

Stos Docker Compose do testowania smart home na symulowanych urządzeniach Zigbee oraz do zbierania
danych do artykułu. Symulator działa na poziomie MQTT (format Zigbee2MQTT), a parametry sieci
(hopy, opóźnienia, utrata pakietów) bierze z profilu, który można zasilić wynikami z ns-3.

## Układ

```
urządzenia (zigbee-sim) ──MQTT──> mosquitto ──> Home Assistant   (automatyzacje, dashboard)
                                       │──────> Node-RED          (własne scenariusze)
                                       └──────> Telegraf -> InfluxDB -> Grafana   (metryki)
(opcjonalnie) prawdziwy dongle -> Zigbee2MQTT ──┘
```

| Usługa | Adres | Rola |
|---|---|---|
| Home Assistant | http://localhost:8123 | smart home |
| Node-RED | http://localhost:1880 | przepływy, wstrzykiwanie zdarzeń |
| Grafana | http://localhost:3000 | dashboardy (login z `.env`) |
| InfluxDB | http://localhost:8086 | szeregi czasowe |
| Mosquitto | localhost:1883 | broker (bez uwierzytelniania, tylko sieć lokalna) |
| Zigbee2MQTT | http://localhost:8080 | tylko profil `hardware` |

## Start

Przed pierwszym uruchomieniem skopiuj `.env.example` jako `.env` i zmień w nim hasła oraz token
(`.env` nie trafia do repozytorium):

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f zigbee-sim
```

W Home Assistant po pierwszym uruchomieniu: Ustawienia → Urządzenia i usługi → Dodaj integrację →
**MQTT**, broker `mosquitto`, port `1883`. Symulowane urządzenia pojawią się same (MQTT discovery).

## Symulator

- Typy urządzeń: `climate_sensor`, `motion_sensor`, `contact_sensor`, `smart_plug` (wtyczka reaguje na
  polecenia z Home Assistanta).
- Urządzenia i parametry sieci: `sim/network_profile.json`. Po zmianie: `docker compose restart zigbee-sim`.
- Powtarzalność: odkomentuj `SIM_SEED` w `docker-compose.yml`.
- Polecenia (jak w Zigbee2MQTT): `zigbee2mqtt/<nazwa>/set` (JSON), `…/set/state` (sam tekst, np. `ON`) i
  `…/get` (`{"state": ""}`). Czujniki bateryjne ich nie przyjmują.
- Stan po `/set` (`SIM_SET_MODE`):
  - `optimistic` (domyślnie, jak `optimistic: true` w Z2M): nowy stan trafia na MQTT od razu, a potwierdzenie
    z urządzenia dochodzi później przez model sieci. Jeśli polecenie zginie w dół, bramka dalej "wie" o nowym
    stanie, a urządzenie się nie zmieni, dopóki nie przyjdzie następny raport.
  - `confirmed`: stan jest publikowany dopiero po potwierdzeniu (opóźnienie w dół + w górę). Przy pomiarze
    opóźnienia polecenie → stan użyj tego trybu, a w prawdziwym Z2M ustaw `optimistic: false`.
  Tryb można nadpisać per urządzenie kluczem `optimistic` w profilu.
- Utrata w dół: `loss_down` per urządzenie w profilu (domyślnie jak `loss`).
- Availability: urządzenie bez wiadomości dłużej niż timeout (`SIM_AVAILABILITY_ACTIVE_S`=600 s dla zasilanych
  z sieci, `SIM_AVAILABILITY_PASSIVE_S`=90000 s dla bateryjnych; per urządzenie `availability_timeout_s`)
  dostaje `{"state":"offline"}` na `…/availability` i wraca po następnej dostarczonej wiadomości.
  Wartości domyślne wzorowane na Z2M, sprawdź je w jego dokumentacji dla swojej wersji.
- `zigbee2mqtt/bridge/state` z `{"state":"online"}` (retained). Z profilem `hardware` ustaw
  `SIM_PUBLISH_BRIDGE=0`, bo ten temat publikuje wtedy prawdziwy Zigbee2MQTT.
- Log `sim/logs/sim_log.csv`: kolumna `direction` to `up` (raporty), `down` (polecenia i `/get`) albo
  `bridge` (stan optymistyczny, zmiany availability). Polecenia do czujników są zapisywane jako
  `command_ignored`. Podsumowanie: `python3 sim/analyze_log.py sim/logs/sim_log.csv`.
- Testy (bez brokera): `python3 sim/test_simulator.py`.
- Nie ma `bridge/devices` ani innych tematów `bridge/*`; Telegraf zbiera tylko `zigbee2mqtt/+` (płaskie nazwy).
- Uwaga: wiadomość o końcu ruchu (`motion_clear`) też może zostać „zgubiona”, wtedy czujnik w Home
  Assistant zostaje w stanie wykrycia. To zamierzone, tak działa niezawodność prawdziwej sieci.

## Wspólny punkt z ns-3

Gotowy scenariusz ns-3 z tą samą topologią co profil jest w katalogu `ns3/` (opis, instalacja,
ograniczenia: `ns3/README.md`). Skrót:

```bash
pip install ns3==3.44
cd ns3 && ./build.sh                      # kompilacja scenariusza
./run_batch.sh 40 wyniki.csv              # 40 krótkich, niezależnych przebiegów
python3 ns3_to_profile.py wyniki.csv ../sim/network_profile.json -o ../sim/network_profile.json
docker compose restart zigbee-sim
```

Tłumienia łączy w `ns3/topology.csv` są założeniami. Skalibruj je na podstawie RSSI/LQI z prawdziwej sieci.

## Analiza wyników

Symulator zapisuje każde zdarzenie do `sim/logs/sim_log.csv`:

```bash
python3 sim/analyze_log.py sim/logs/sim_log.csv
```

Przykładowe zapytanie Flux w Grafanie (temperatura per urządzenie):

```flux
from(bucket: "zigbee")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "zigbee2mqtt" and r._field == "temperature")
```

## Test hybrydowy z prawdziwym dongle'em

```bash
docker compose --profile hardware up -d
```

Ustaw ścieżkę urządzenia w `docker-compose.yml` (`devices:`) i w `zigbee2mqtt/data/configuration.yaml`.
Prawdziwe urządzenia i symulowane współistnieją na tym samym brokerze (inne nazwy, ten sam format).

## Zanim użyjesz w badaniach

- Przypnij wersje obrazów (`docker compose images`) i zapisz je w metodologii.
- Zmień hasła i token w `.env`. Broker MQTT jest otwarty, więc nie wystawiaj go poza sieć lokalną.
- Format wiadomości odwzorowuje Zigbee2MQTT, ale to przybliżenie. Porównaj z wiadomościami prawdziwych urządzeń.
