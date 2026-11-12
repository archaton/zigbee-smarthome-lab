#!/usr/bin/env python3
"""Symulator urządzeń Zigbee na poziomie MQTT (format wiadomości jak w Zigbee2MQTT).

Nie symuluje radia ani stosu Zigbee. Odtwarza to, co widzi aplikacja: JSON-y na
tematach <base>/<nazwa_urządzenia> oraz wpisy MQTT discovery dla Home Assistanta.
Parametry sieci (liczba hopów, opóźnienie, utrata pakietów, linkquality) pochodzą
z network_profile.json, który można wypełnić wynikami z symulatora sieciowego (np. ns-3).

Każde zdarzenie jest logowane do CSV (SIM_LOG), co daje dane do analizy w artykule.

Zachowanie bliskie Zigbee2MQTT:
- polecenia: <base>/<nazwa>/set (JSON), /set/state (sam tekst, np. ON) i /get ({"state": ""});
- po /set stan może być publikowany optymistycznie od razu (SIM_SET_MODE=optimistic, jak domyślnie
  w Z2M) albo dopiero po potwierdzeniu z urządzenia (confirmed); polecenie może zginąć w dół;
- availability: <base>/<nazwa>/availability, urządzenie bez wiadomości dłużej niż timeout jest offline;
- <base>/bridge/state z JSON-em {"state":"online"} (retained).
Kolumna direction w logu: up (urządzenie -> bramka), down (bramka -> urządzenie),
bridge (zdarzenia po stronie bramki: stan optymistyczny, zmiany availability).
"""
import csv
import json
import math
import os
import random
import signal
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
BASE = os.getenv("SIM_BASE_TOPIC", "zigbee2mqtt").strip("/")
DISCOVERY_PREFIX = os.getenv("SIM_DISCOVERY_PREFIX", "homeassistant")
PROFILE_PATH = os.getenv("SIM_PROFILE", "/app/network_profile.json")
LOG_PATH = os.getenv("SIM_LOG", "/logs/sim_log.csv")
REPORT_INTERVAL = float(os.getenv("SIM_REPORT_INTERVAL", "30"))
TICK = 0.5

# Tryb odpowiedzi na polecenia /set (jak opcja `optimistic` w Zigbee2MQTT, domyślnie włączona):
#   optimistic - bramka publikuje nowy stan od razu po odebraniu polecenia, a potem, po drodze
#                przez sieć, stan potwierdzony przez urządzenie (może się zgubić lub różnić)
#   confirmed  - stan jest publikowany dopiero po potwierdzeniu z urządzenia (opóźnienie w dół + w górę)
# Można nadpisać per urządzenie kluczem "optimistic": true/false w profilu.
SET_MODE = os.getenv("SIM_SET_MODE", "optimistic").strip().lower()
if SET_MODE not in ("optimistic", "confirmed"):
    raise SystemExit(f"SIM_SET_MODE musi być 'optimistic' albo 'confirmed', jest: {SET_MODE!r}")

# Czas bez żadnej wiadomości od urządzenia, po którym uznajemy je za niedostępne (jak availability
# w Zigbee2MQTT: krótszy dla urządzeń zasilanych z sieci, bardzo długi dla bateryjnych).
# Per urządzenie: "availability_timeout_s" w profilu (0 wyłącza).
AVAIL_ACTIVE_S = float(os.getenv("SIM_AVAILABILITY_ACTIVE_S", "600"))
AVAIL_PASSIVE_S = float(os.getenv("SIM_AVAILABILITY_PASSIVE_S", "90000"))

# Stan "bramki" jak w Zigbee2MQTT: <base>/bridge/state z JSON-em {"state":"online"} (retained).
# Wyłącz (0), gdy obok symulatora działa prawdziwy Zigbee2MQTT (profil hardware), bo on publikuje ten temat.
PUBLISH_BRIDGE = os.getenv("SIM_PUBLISH_BRIDGE", "1") != "0"
BRIDGE_STATE_TOPIC = f"{BASE}/bridge/state"

stop = threading.Event()
connected = threading.Event()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- profil


def load_profile(path):
    with open(path, encoding="utf-8") as f:
        profile = json.load(f)
    profile.setdefault("latency_ms_per_hop", {"mean": 12, "jitter": 5})
    profile.setdefault("loss_per_hop", 0.01)
    profile.setdefault("linkquality", {"base": 255, "drop_per_hop": 40})
    return profile


class EventLog:
    """Log zdarzeń w CSV: kiedy, które urządzenie, ile hopów, czy zgubiono, jakie opóźnienie."""

    def __init__(self, path):
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._f = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        if is_new:
            self._w.writerow(["ts_utc", "device", "hops", "direction", "event", "dropped", "delay_ms"])
            self._f.flush()

    def write(self, *row):
        with self._lock:
            self._w.writerow(row)
            self._f.flush()

    def close(self):
        with self._lock:
            self._f.close()


class Link:
    """Model łącza urządzenie <-> koordynator, wyprowadzony z liczby hopów lub nadpisany w profilu."""

    def __init__(self, profile, cfg):
        self.hops = max(1, int(cfg.get("hops", 1)))
        per_hop = profile["latency_ms_per_hop"]
        self.mean = float(cfg.get("latency_ms", per_hop["mean"] * self.hops))
        self.jitter = float(cfg.get("latency_jitter_ms", per_hop["jitter"] * math.sqrt(self.hops)))
        self.loss = float(cfg.get("loss", 1 - (1 - profile["loss_per_hop"]) ** self.hops))
        # utrata polecenia w dół (bramka -> urządzenie); domyślnie taka sama jak w górę
        self.loss_down = float(cfg.get("loss_down", self.loss))
        lq = profile["linkquality"]
        self.lq_base = float(lq["base"])
        self.lq_drop = float(lq["drop_per_hop"])

    def delay_ms(self):
        return max(1.0, random.gauss(self.mean, self.jitter))

    def lost(self):
        return random.random() < self.loss

    def lost_down(self):
        return random.random() < self.loss_down

    def linkquality(self):
        value = self.lq_base - (self.hops - 1) * self.lq_drop + random.gauss(0, 8)
        return int(min(255, max(0, value)))


# --------------------------------------------------------------------------- urządzenia


class Device:
    type = "generic"
    accepts_commands = False  # czy urządzenie reaguje na /set i /get (czujniki bateryjne: nie)
    mains_powered = False     # zasilane z sieci = router; wpływa na domyślny timeout availability

    def __init__(self, cfg, profile, client, log):
        self.name = cfg["name"]
        self.uid = f"sim_{self.name}"
        self.topic = f"{BASE}/{self.name}"
        self.cfg = cfg
        self.client = client
        self.log = log
        self.link = Link(profile, cfg)
        self.battery = random.uniform(70, 100)
        self.next_report = time.time() + random.uniform(1, REPORT_INTERVAL)
        self._order_lock = threading.Lock()
        self._last_deliver_at = 0.0
        self.optimistic = bool(cfg.get("optimistic", SET_MODE == "optimistic"))
        default_timeout = AVAIL_ACTIVE_S if self.mains_powered else AVAIL_PASSIVE_S
        self.avail_timeout = float(cfg.get("availability_timeout_s", default_timeout))
        self.last_seen = time.time()
        self.available = None  # None = jeszcze nie opublikowano
        self._avail_lock = threading.Lock()

    # -- wysyłanie stanu przez model sieci
    def state(self):
        raise NotImplementedError

    def emit(self, event):
        payload = self.state()
        payload["linkquality"] = self.link.linkquality()
        dropped = self.link.lost()
        now = time.time()
        with self._order_lock:
            # kolejność dostarczania z jednego urządzenia jest zachowana (jak w sieci Zigbee),
            # więc opóźnienie efektywne może być większe niż wylosowane
            deliver_at = max(now + self.link.delay_ms() / 1000.0, self._last_deliver_at + 0.001)
            if not dropped:
                self._last_deliver_at = deliver_at
        delay_ms = (deliver_at - now) * 1000.0
        self.log.write(utc_now(), self.name, self.link.hops, "up", event, int(dropped), round(delay_ms, 1))
        if dropped:
            return
        timer = threading.Timer(delay_ms / 1000.0, self._publish, args=(payload,))
        timer.daemon = True
        timer.start()

    def _publish(self, payload):
        """Wiadomość z urządzenia dotarła do bramki: publikacja, odświeżenie last_seen i dostępności."""
        self.client.publish(self.topic, json.dumps(payload), qos=0, retain=False)
        self.last_seen = time.time()
        self.on_delivered(payload)
        if self.available is False:
            self.set_availability("online")

    def on_delivered(self, payload):
        """Hook: urządzenie może zaktualizować pamięć podręczną bramki."""

    def check_availability(self, now):
        """Jak availability w Zigbee2MQTT: brak wiadomości dłużej niż timeout = urządzenie offline."""
        if self.avail_timeout > 0 and self.available and now - self.last_seen > self.avail_timeout:
            self.set_availability("offline")

    def schedule_report(self, factor=1.0):
        self.next_report = time.time() + REPORT_INTERVAL * factor * random.uniform(0.8, 1.2)

    def drain_battery(self):
        self.battery = max(1.0, self.battery - random.uniform(0, 0.02))

    def tick(self, now):
        pass

    # -- polecenia /set, /set/state, /get
    def parse_set(self, payload):
        """Zwraca żądany stan (True/False/"TOGGLE") albo None, gdy polecenie jest nieobsługiwane."""
        return None

    def receive_command(self, kind, payload):
        """Polecenie dotarło do bramki. W trybie optymistycznym bramka od razu publikuje nowy stan,
        a samo polecenie idzie w dół przez model łącza (opóźnienie, możliwa utrata)."""
        wanted = None
        if kind != "get":
            wanted = self.parse_set(payload)
            if wanted is None:
                return
            if self.optimistic:
                self.publish_optimistic(wanted)
        delay_ms = self.link.delay_ms()
        dropped = self.link.lost_down()
        self.log.write(utc_now(), self.name, self.link.hops, "down",
                       "get" if kind == "get" else "command", int(dropped), round(delay_ms, 1))
        if dropped:
            return
        timer = threading.Timer(delay_ms / 1000.0, self.apply_command, args=(kind, wanted))
        timer.daemon = True
        timer.start()

    def publish_optimistic(self, wanted):
        pass

    def apply_command(self, kind, wanted):
        pass

    # -- discovery dla Home Assistanta
    def _entity(self, obj, **extra):
        cfg = {
            "unique_id": f"{self.uid}_{obj}",
            "state_topic": self.topic,
            "availability": [
                {"topic": f"{self.topic}/availability", "value_template": "{{ value_json.state }}"},
                {"topic": BRIDGE_STATE_TOPIC, "value_template": "{{ value_json.state }}"},
            ],
            "availability_mode": "all",
            "device": {
                "identifiers": [self.uid],
                "name": self.name,
                "manufacturer": "Simulated Zigbee",
                "model": self.type,
            },
        }
        cfg.update(extra)
        return cfg

    def _battery_entities(self):
        yield "sensor", "battery", self._entity(
            "battery", name="Battery", device_class="battery", unit_of_measurement="%",
            state_class="measurement", entity_category="diagnostic",
            value_template="{{ value_json.battery }}")
        yield "sensor", "linkquality", self._entity(
            "linkquality", name="Linkquality", unit_of_measurement="lqi",
            state_class="measurement", entity_category="diagnostic",
            value_template="{{ value_json.linkquality }}")

    def entities(self):
        return []

    def publish_discovery(self):
        for component, obj, cfg in self.entities():
            topic = f"{DISCOVERY_PREFIX}/{component}/{self.uid}/{obj}/config"
            self.client.publish(topic, json.dumps(cfg), qos=1, retain=True)
        self.set_availability("online")

    def set_availability(self, state, log_event=True):
        with self._avail_lock:
            previous = self.available
            self.available = state == "online"
            if state == "online":
                self.last_seen = time.time()
            self.client.publish(f"{self.topic}/availability", json.dumps({"state": state}), qos=1, retain=True)
        if log_event and previous is not None and previous != self.available:
            self.log.write(utc_now(), self.name, self.link.hops, "bridge", f"availability_{state}", 0, 0.0)


class ClimateSensor(Device):
    type = "climate_sensor"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.temperature = random.uniform(19, 23)
        self.humidity = random.uniform(40, 55)

    def state(self):
        return {
            "temperature": round(self.temperature, 2),
            "humidity": round(self.humidity, 1),
            "battery": int(round(self.battery)),
            "voltage": int(2500 + self.battery * 5),
        }

    def tick(self, now):
        if now < self.next_report:
            return
        self.temperature += (21.0 - self.temperature) * 0.02 + random.gauss(0, 0.1)
        self.humidity = min(80.0, max(20.0, self.humidity + (45.0 - self.humidity) * 0.02 + random.gauss(0, 0.3)))
        self.drain_battery()
        self.emit("report")
        self.schedule_report()

    def entities(self):
        yield "sensor", "temperature", self._entity(
            "temperature", name="Temperature", device_class="temperature", unit_of_measurement="°C",
            state_class="measurement", value_template="{{ value_json.temperature }}")
        yield "sensor", "humidity", self._entity(
            "humidity", name="Humidity", device_class="humidity", unit_of_measurement="%",
            state_class="measurement", value_template="{{ value_json.humidity }}")
        yield from self._battery_entities()


class MotionSensor(Device):
    type = "motion_sensor"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.occupancy = False
        self.events_per_hour = float(self.cfg.get("events_per_hour", 12))
        self.timeout_s = float(self.cfg.get("occupancy_timeout_s", 30))
        self.schedule_report(10.0)  # rzadki raport kontrolny (bateria)

    def state(self):
        return {"occupancy": self.occupancy, "battery": int(round(self.battery))}

    def tick(self, now):
        if not self.occupancy and random.random() < self.events_per_hour / 3600.0 * TICK:
            self.occupancy = True
            self.emit("motion")
            timer = threading.Timer(self.timeout_s, self._clear)
            timer.daemon = True
            timer.start()
        elif now >= self.next_report:
            self.drain_battery()
            self.emit("heartbeat")
            self.schedule_report(10.0)

    def _clear(self):
        self.occupancy = False
        self.emit("motion_clear")  # ta wiadomość też może zostać zgubiona, jak w prawdziwej sieci

    def entities(self):
        yield "binary_sensor", "occupancy", self._entity(
            "occupancy", name="Occupancy", device_class="occupancy",
            value_template="{{ 'ON' if value_json.occupancy else 'OFF' }}")
        yield from self._battery_entities()


class ContactSensor(Device):
    type = "contact_sensor"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.contact = True  # True = zamknięte
        self.events_per_hour = float(self.cfg.get("events_per_hour", 4))
        self.schedule_report(10.0)

    def state(self):
        return {"contact": self.contact, "battery": int(round(self.battery))}

    def tick(self, now):
        if random.random() < self.events_per_hour / 3600.0 * TICK:
            self.contact = not self.contact
            self.emit("open" if not self.contact else "close")
        elif now >= self.next_report:
            self.drain_battery()
            self.emit("heartbeat")
            self.schedule_report(10.0)

    def entities(self):
        yield "binary_sensor", "contact", self._entity(
            "contact", name="Door", device_class="door",
            value_template="{{ 'OFF' if value_json.contact else 'ON' }}")
        yield from self._battery_entities()


class SmartPlug(Device):
    type = "smart_plug"
    accepts_commands = True
    mains_powered = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.on = str(self.cfg.get("initial_state", "OFF")).upper() == "ON"  # prawdziwy stan urządzenia
        self.cached_on = self.on  # stan znany bramce (ostatnia publikacja), może się różnić od prawdziwego
        self.load_w = float(self.cfg.get("load_w", 60))
        self.energy_kwh = 0.0
        self._last_energy_ts = time.time()

    def _power(self, on):
        return round(max(0.0, random.gauss(self.load_w, self.load_w * 0.05)), 1) if on else 0.0

    def state(self, on=None):
        on = self.on if on is None else on
        now = time.time()
        power = self._power(on)
        self.energy_kwh += power / 1000.0 * (now - self._last_energy_ts) / 3600.0
        self._last_energy_ts = now
        return {"state": "ON" if on else "OFF", "power": power, "energy": round(self.energy_kwh, 4)}

    def tick(self, now):
        if now >= self.next_report:
            self.emit("report")
            self.schedule_report()

    def on_delivered(self, payload):
        self.cached_on = payload.get("state") == "ON"

    def parse_set(self, payload):
        wanted = str(payload.get("state", "")).strip().upper()
        if wanted == "TOGGLE":
            return "TOGGLE"
        if wanted in ("ON", "OFF"):
            return wanted == "ON"
        return None

    def publish_optimistic(self, wanted):
        """Jak optimistic w Zigbee2MQTT: nowy stan trafia na MQTT od razu, bez udziału sieci radiowej.
        Dla TOGGLE bramka odwraca stan, który zna (pamięć podręczna), a nie prawdziwy stan urządzenia."""
        on = (not self.cached_on) if wanted == "TOGGLE" else wanted
        self.cached_on = on
        payload = self.state(on=on)
        payload["linkquality"] = self.link.linkquality()
        self.client.publish(self.topic, json.dumps(payload), qos=0, retain=False)
        self.log.write(utc_now(), self.name, self.link.hops, "bridge", "set_optimistic", 0, 0.0)

    def apply_command(self, kind, wanted):
        """Polecenie dotarło do urządzenia: zmiana stanu i raport w górę (albo odpowiedź na /get)."""
        if kind == "get":
            self.emit("get_reply")
            return
        self.on = (not self.on) if wanted == "TOGGLE" else wanted
        self.emit("set_ack")

    def entities(self):
        yield "switch", "switch", self._entity(
            "switch", name="Switch", command_topic=f"{self.topic}/set",
            payload_on='{"state": "ON"}', payload_off='{"state": "OFF"}',
            state_on="ON", state_off="OFF", value_template="{{ value_json.state }}")
        yield "sensor", "power", self._entity(
            "power", name="Power", device_class="power", unit_of_measurement="W",
            state_class="measurement", value_template="{{ value_json.power }}")
        yield "sensor", "energy", self._entity(
            "energy", name="Energy", device_class="energy", unit_of_measurement="kWh",
            state_class="total_increasing", value_template="{{ value_json.energy }}")
        yield from self._battery_entities_no_battery()

    def _battery_entities_no_battery(self):
        # wtyczka sieciowa nie ma baterii, tylko linkquality
        yield "sensor", "linkquality", self._entity(
            "linkquality", name="Linkquality", unit_of_measurement="lqi",
            state_class="measurement", entity_category="diagnostic",
            value_template="{{ value_json.linkquality }}")


DEVICE_TYPES = {cls.type: cls for cls in (ClimateSensor, MotionSensor, ContactSensor, SmartPlug)}


# --------------------------------------------------------------------------- polecenia


def build_routes(devices):
    """Tematy poleceń per urządzenie: <base>/<nazwa>/set, /set/state (sam tekst, np. ON) i /get.
    Subskrypcje są jawne (a nie <base>/+/set), więc działają też nazwy z ukośnikami."""
    routes = {}
    for dev in devices.values():
        routes[f"{dev.topic}/set"] = (dev, "set")
        routes[f"{dev.topic}/set/state"] = (dev, "set_state")
        routes[f"{dev.topic}/get"] = (dev, "get")
    return routes


def parse_command(kind, raw):
    """Zwraca słownik polecenia albo None, gdy treść jest nie do przyjęcia."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    except UnicodeDecodeError:
        return None
    if kind == "set_state":
        return {"state": text.strip().strip('"')}
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def handle_command(dev, kind, raw, log):
    """Polecenie MQTT dla urządzenia. Czujniki bateryjne nie przyjmują poleceń: zapisujemy to jako
    osobne zdarzenie "command_ignored", żeby nie fałszowało statystyk poleceń w dół."""
    payload = parse_command(kind, raw)
    if payload is None:
        return
    if not dev.accepts_commands:
        log.write(utc_now(), dev.name, dev.link.hops, "down", "command_ignored", 0, 0.0)
        return
    dev.receive_command(kind, payload)


def publish_bridge_state(client, state):
    return client.publish(BRIDGE_STATE_TOPIC, json.dumps({"state": state}), qos=1, retain=True)


# --------------------------------------------------------------------------- główny program


def main():
    seed = os.getenv("SIM_SEED")
    if seed:
        random.seed(int(seed))

    profile = load_profile(PROFILE_PATH)
    log = EventLog(LOG_PATH)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"zigbee-sim-{os.getpid()}")
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    if PUBLISH_BRIDGE:
        client.will_set(BRIDGE_STATE_TOPIC, json.dumps({"state": "offline"}), qos=1, retain=True)

    devices = {}
    for cfg in profile["devices"]:
        cls = DEVICE_TYPES.get(cfg["type"])
        if cls is None:
            raise SystemExit(f"Nieznany typ urządzenia: {cfg['type']} (dostępne: {sorted(DEVICE_TYPES)})")
        if cfg["name"] in devices:
            raise SystemExit(f"Zduplikowana nazwa urządzenia: {cfg['name']}")
        devices[cfg["name"]] = cls(cfg, profile, client, log)
    routes = build_routes(devices)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code != 0:
            print(f"Połączenie z brokerem odrzucone: {reason_code}", flush=True)
            return
        print(f"Połączono z {MQTT_HOST}:{MQTT_PORT}, urządzeń: {len(devices)}, "
              f"tryb /set: {SET_MODE}", flush=True)
        if PUBLISH_BRIDGE:
            publish_bridge_state(c, "online")
        for topic in routes:
            c.subscribe(topic, qos=1)
        for dev in devices.values():
            dev.publish_discovery()
        connected.set()

    def on_disconnect(c, userdata, flags, reason_code, properties):
        connected.clear()

    def on_message(c, userdata, msg):
        route = routes.get(msg.topic)
        if route is not None:
            handle_command(route[0], route[1], msg.payload, log)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    def shutdown(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while not stop.wait(TICK):
        if not connected.is_set():
            continue
        now = time.time()
        for dev in devices.values():
            dev.tick(now)
            dev.check_availability(now)

    if connected.is_set():
        for dev in devices.values():
            dev.set_availability("offline", log_event=False)  # zamknięcie symulatora, nie awaria
        if PUBLISH_BRIDGE:
            publish_bridge_state(client, "offline").wait_for_publish(timeout=3)
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
