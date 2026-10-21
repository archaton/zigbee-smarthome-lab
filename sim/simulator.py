#!/usr/bin/env python3
"""Symulator urządzeń Zigbee na poziomie MQTT (format wiadomości jak w Zigbee2MQTT).

Nie symuluje radia ani stosu Zigbee. Odtwarza to, co widzi aplikacja: JSON-y na
tematach <base>/<nazwa_urządzenia> oraz wpisy MQTT discovery dla Home Assistanta.
Parametry sieci (liczba hopów, opóźnienie, utrata pakietów, linkquality) pochodzą
z network_profile.json, który można wypełnić wynikami z symulatora sieciowego (np. ns-3).

Każde zdarzenie jest logowane do CSV (SIM_LOG), co daje dane do analizy w artykule.
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
SIM_STATE_TOPIC = f"{BASE}-sim/state"  # poza zakresem tematów zbieranych przez Telegraf
TICK = 0.5

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


class Link:
    """Model łącza urządzenie <-> koordynator, wyprowadzony z liczby hopów lub nadpisany w profilu."""

    def __init__(self, profile, cfg):
        self.hops = max(1, int(cfg.get("hops", 1)))
        per_hop = profile["latency_ms_per_hop"]
        self.mean = float(cfg.get("latency_ms", per_hop["mean"] * self.hops))
        self.jitter = float(cfg.get("latency_jitter_ms", per_hop["jitter"] * math.sqrt(self.hops)))
        self.loss = float(cfg.get("loss", 1 - (1 - profile["loss_per_hop"]) ** self.hops))
        lq = profile["linkquality"]
        self.lq_base = float(lq["base"])
        self.lq_drop = float(lq["drop_per_hop"])

    def delay_ms(self):
        return max(1.0, random.gauss(self.mean, self.jitter))

    def lost(self):
        return random.random() < self.loss

    def linkquality(self):
        value = self.lq_base - (self.hops - 1) * self.lq_drop + random.gauss(0, 8)
        return int(min(255, max(0, value)))


# --------------------------------------------------------------------------- urządzenia


class Device:
    type = "generic"

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
        timer = threading.Timer(delay_ms / 1000.0, self._publish, args=(json.dumps(payload),))
        timer.daemon = True
        timer.start()

    def _publish(self, body):
        self.client.publish(self.topic, body, qos=0, retain=False)

    def schedule_report(self, factor=1.0):
        self.next_report = time.time() + REPORT_INTERVAL * factor * random.uniform(0.8, 1.2)

    def drain_battery(self):
        self.battery = max(1.0, self.battery - random.uniform(0, 0.02))

    def tick(self, now):
        pass

    def handle_set(self, payload):
        pass

    # -- discovery dla Home Assistanta
    def _entity(self, obj, **extra):
        cfg = {
            "unique_id": f"{self.uid}_{obj}",
            "state_topic": self.topic,
            "availability": [
                {"topic": f"{self.topic}/availability", "value_template": "{{ value_json.state }}"},
                {"topic": SIM_STATE_TOPIC},
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

    def set_availability(self, state):
        self.client.publish(f"{self.topic}/availability", json.dumps({"state": state}), qos=1, retain=True)


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.on = str(self.cfg.get("initial_state", "OFF")).upper() == "ON"
        self.load_w = float(self.cfg.get("load_w", 60))
        self.energy_kwh = 0.0
        self._last_energy_ts = time.time()

    def _power(self):
        return round(max(0.0, random.gauss(self.load_w, self.load_w * 0.05)), 1) if self.on else 0.0

    def state(self):
        now = time.time()
        power = self._power()
        self.energy_kwh += power / 1000.0 * (now - self._last_energy_ts) / 3600.0
        self._last_energy_ts = now
        return {"state": "ON" if self.on else "OFF", "power": power, "energy": round(self.energy_kwh, 4)}

    def tick(self, now):
        if now >= self.next_report:
            self.emit("report")
            self.schedule_report()

    def handle_set(self, payload):
        """Polecenie z Home Assistanta: opóźnienie w dół, potem raport stanu przez model sieci."""
        wanted = str(payload.get("state", "")).upper()
        if wanted == "TOGGLE":
            self.on = not self.on
        elif wanted in ("ON", "OFF"):
            self.on = wanted == "ON"
        else:
            return
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


# --------------------------------------------------------------------------- główny program


def main():
    seed = os.getenv("SIM_SEED")
    if seed:
        random.seed(int(seed))

    profile = load_profile(PROFILE_PATH)
    log = EventLog(LOG_PATH)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"zigbee-sim-{os.getpid()}")
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.will_set(SIM_STATE_TOPIC, "offline", qos=1, retain=True)

    devices = {}
    for cfg in profile["devices"]:
        cls = DEVICE_TYPES.get(cfg["type"])
        if cls is None:
            raise SystemExit(f"Nieznany typ urządzenia: {cfg['type']} (dostępne: {sorted(DEVICE_TYPES)})")
        if cfg["name"] in devices:
            raise SystemExit(f"Zduplikowana nazwa urządzenia: {cfg['name']}")
        devices[cfg["name"]] = cls(cfg, profile, client, log)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code != 0:
            print(f"Połączenie z brokerem odrzucone: {reason_code}", flush=True)
            return
        print(f"Połączono z {MQTT_HOST}:{MQTT_PORT}, urządzeń: {len(devices)}", flush=True)
        c.publish(SIM_STATE_TOPIC, "online", qos=1, retain=True)
        c.subscribe(f"{BASE}/+/set", qos=1)
        for dev in devices.values():
            dev.publish_discovery()
        connected.set()

    def on_disconnect(c, userdata, flags, reason_code, properties):
        connected.clear()

    def on_message(c, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) != 3 or parts[2] != "set":
            return
        dev = devices.get(parts[1])
        if dev is None:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        # polecenie w dół też idzie przez model łącza (opóźnienie), bez utraty (retry na poziomie APS)
        delay_ms = dev.link.delay_ms()
        log.write(utc_now(), dev.name, dev.link.hops, "down", "command", 0, round(delay_ms, 1))
        timer = threading.Timer(delay_ms / 1000.0, dev.handle_set, args=(payload,))
        timer.daemon = True
        timer.start()

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

    if connected.is_set():
        for dev in devices.values():
            dev.set_availability("offline")
        client.publish(SIM_STATE_TOPIC, "offline", qos=1, retain=True).wait_for_publish(timeout=3)
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
