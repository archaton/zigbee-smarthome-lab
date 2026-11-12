#!/usr/bin/env python3
"""Testy symulatora bez brokera MQTT (fałszywy klient zapisuje publikacje).

Uruchomienie:  python3 sim/test_simulator.py   (z katalogu głównego projektu, bez brokera i bez paho)

Główny przypadek: polecenie /set dla wtyczki w trybie optymistycznym i potwierdzonym, z utratą
polecenia w dół i bez niej. Testy używają prawdziwych timerów (opóźnienia rzędu 60 ms).
"""
import csv
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:  # symulator importuje paho, ale testy go nie potrzebują
    import paho.mqtt.client  # noqa: F401
except ImportError:
    paho = types.ModuleType("paho")
    paho_mqtt = types.ModuleType("paho.mqtt")
    paho_client = types.ModuleType("paho.mqtt.client")
    paho.mqtt = paho_mqtt
    paho_mqtt.client = paho_client
    sys.modules.update({"paho": paho, "paho.mqtt": paho_mqtt, "paho.mqtt.client": paho_client})

import simulator as sim  # noqa: E402

DELAY_MS = 60.0  # opóźnienie w jedną stronę w testach (jitter = 0)


class FakeInfo:
    def wait_for_publish(self, timeout=None):
        return True


class FakeClient:
    """Zapisuje publikacje: (czas, temat, treść jako tekst, retain)."""

    def __init__(self):
        self.messages = []
        self._lock = threading.Lock()

    def publish(self, topic, payload=None, qos=0, retain=False):
        with self._lock:
            self.messages.append((time.time(), topic, payload, retain))
        return FakeInfo()

    def on_topic(self, topic):
        with self._lock:
            return [m for m in self.messages if m[1] == topic]


def wait_for(predicate, timeout=1.5):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


PROFILE = {
    "latency_ms_per_hop": {"mean": 12, "jitter": 5},
    "loss_per_hop": 0.0,
    "linkquality": {"base": 255, "drop_per_hop": 40},
}


class SimTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = os.path.join(self.tmp.name, "log.csv")
        self.log = sim.EventLog(self.log_path)
        self.addCleanup(self.log.close)
        self.client = FakeClient()

    def make(self, cls, name="plug", **cfg):
        base = {"name": name, "hops": 1, "latency_ms": DELAY_MS, "latency_jitter_ms": 0.0, "loss": 0.0}
        base.update(cfg)
        return cls(base, PROFILE, self.client, self.log)

    def plug(self, **cfg):
        cfg.setdefault("initial_state", "OFF")
        return self.make(sim.SmartPlug, **cfg)

    def events(self):
        with open(self.log_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def event_names(self):
        return [(r["direction"], r["event"], r["dropped"]) for r in self.events()]

    def states(self, dev):
        out = []
        for ts, _, body, _ in self.client.on_topic(dev.topic):
            out.append((ts, json.loads(body)))
        return out

    def send(self, dev, kind, raw):
        sim.handle_command(dev, kind, raw, self.log)


class SetModeTests(SimTestCase):
    def test_confirmed_mode_publishes_state_only_after_roundtrip(self):
        dev = self.plug(optimistic=False)
        t0 = time.time()
        self.send(dev, "set", b'{"state": "ON"}')
        time.sleep(0.03)
        self.assertEqual(self.states(dev), [], "w trybie confirmed nie ma stanu przed potwierdzeniem")
        self.assertTrue(wait_for(lambda: len(self.states(dev)) == 1))
        ts, state = self.states(dev)[0]
        self.assertEqual(state["state"], "ON")
        self.assertGreaterEqual(ts - t0, 2 * DELAY_MS / 1000.0 - 0.01, "opóźnienie w dół + w górę")
        self.assertTrue(dev.on)
        self.assertIn(("down", "command", "0"), self.event_names())
        self.assertIn(("up", "set_ack", "0"), self.event_names())
        self.assertNotIn("set_optimistic", [e[1] for e in self.event_names()])

    def test_optimistic_mode_publishes_immediately_then_confirms(self):
        dev = self.plug(optimistic=True)
        t0 = time.time()
        self.send(dev, "set", b'{"state": "ON"}')
        first = self.states(dev)
        self.assertEqual(len(first), 1, "stan optymistyczny od razu po poleceniu")
        self.assertEqual(first[0][1]["state"], "ON")
        self.assertLess(first[0][0] - t0, 0.03)
        self.assertIn("linkquality", first[0][1])
        self.assertFalse(dev.on, "urządzenie jeszcze nie dostało polecenia")
        self.assertTrue(wait_for(lambda: len(self.states(dev)) == 2))
        ts, confirmed = self.states(dev)[1]
        self.assertEqual(confirmed["state"], "ON")
        self.assertGreaterEqual(ts - t0, 2 * DELAY_MS / 1000.0 - 0.01)
        self.assertTrue(dev.on)
        names = [e[1] for e in self.event_names()]
        self.assertEqual(names.count("set_optimistic"), 1)
        self.assertIn(("bridge", "set_optimistic", "0"), self.event_names())
        self.assertIn(("up", "set_ack", "0"), self.event_names())

    def test_optimistic_with_lost_downlink_leaves_device_unchanged(self):
        dev = self.plug(optimistic=True, loss_down=1.0)
        self.send(dev, "set", b'{"state": "ON"}')
        self.assertEqual([s["state"] for _, s in self.states(dev)], ["ON"])
        time.sleep(4 * DELAY_MS / 1000.0)
        self.assertEqual(len(self.states(dev)), 1, "brak potwierdzenia z urządzenia")
        self.assertFalse(dev.on, "polecenie zgubione w dół, więc urządzenie bez zmiany")
        self.assertTrue(dev.cached_on, "bramka dalej uważa, że jest ON (rozjazd jak w Z2M)")
        self.assertIn(("down", "command", "1"), self.event_names())
        self.assertNotIn("set_ack", [e[1] for e in self.event_names()])

    def test_confirmed_with_lost_downlink_publishes_nothing(self):
        dev = self.plug(optimistic=False, loss_down=1.0)
        self.send(dev, "set", b'{"state": "ON"}')
        time.sleep(4 * DELAY_MS / 1000.0)
        self.assertEqual(self.states(dev), [])
        self.assertFalse(dev.on)
        self.assertIn(("down", "command", "1"), self.event_names())

    def test_lost_uplink_ack_in_confirmed_mode(self):
        dev = self.plug(optimistic=False, loss=1.0, loss_down=0.0)
        self.send(dev, "set", b'{"state": "ON"}')
        self.assertTrue(wait_for(lambda: dev.on))
        time.sleep(2 * DELAY_MS / 1000.0)
        self.assertEqual(self.states(dev), [], "potwierdzenie zgubione w górę")
        self.assertIn(("up", "set_ack", "1"), self.event_names())

    def test_default_loss_down_equals_uplink_loss(self):
        dev = self.plug(loss=0.25)
        self.assertEqual(dev.link.loss_down, 0.25)
        dev2 = self.plug(loss=0.25, loss_down=0.0)
        self.assertEqual(dev2.link.loss_down, 0.0)

    def test_toggle_optimistic_inverts_cached_state(self):
        dev = self.plug(optimistic=True, initial_state="OFF")
        self.send(dev, "set", b'{"state": "TOGGLE"}')
        self.assertEqual(self.states(dev)[0][1]["state"], "ON")
        self.assertTrue(wait_for(lambda: len(self.states(dev)) == 2))
        self.assertTrue(dev.on)
        self.send(dev, "set", b'{"state": "TOGGLE"}')
        self.assertEqual(self.states(dev)[2][1]["state"], "OFF")
        self.assertTrue(wait_for(lambda: len(self.states(dev)) == 4))
        self.assertFalse(dev.on)

    def test_mode_comes_from_module_default_when_profile_silent(self):
        dev = self.plug()
        self.assertEqual(dev.optimistic, sim.SET_MODE == "optimistic")


class CommandTopicTests(SimTestCase):
    def test_set_state_plain_text(self):
        dev = self.plug(optimistic=False)
        self.send(dev, "set_state", b"ON")
        self.assertTrue(wait_for(lambda: len(self.states(dev)) == 1))
        self.assertEqual(self.states(dev)[0][1]["state"], "ON")
        self.send(dev, "set_state", b'"off"')
        self.assertTrue(wait_for(lambda: len(self.states(dev)) == 2))
        self.assertEqual(self.states(dev)[1][1]["state"], "OFF")

    def test_get_replies_with_current_state(self):
        dev = self.plug(optimistic=False, initial_state="ON")
        t0 = time.time()
        self.send(dev, "get", b'{"state": ""}')
        self.assertTrue(wait_for(lambda: len(self.states(dev)) == 1))
        ts, state = self.states(dev)[0]
        self.assertEqual(state["state"], "ON")
        self.assertGreaterEqual(ts - t0, 2 * DELAY_MS / 1000.0 - 0.01)
        self.assertIn(("down", "get", "0"), self.event_names())
        self.assertIn(("up", "get_reply", "0"), self.event_names())

    def test_get_does_not_publish_optimistically(self):
        dev = self.plug(optimistic=True)
        self.send(dev, "get", b'{"state": ""}')
        self.assertEqual(self.states(dev), [])

    def test_invalid_payloads_are_ignored(self):
        dev = self.plug(optimistic=True)
        for raw in (b"not json", b"[1, 2]", b'{"state": "BANANA"}', b'{"brightness": 10}', b"\xff\xfe"):
            self.send(dev, "set", raw)
        time.sleep(0.05)
        self.assertEqual(self.states(dev), [])
        self.assertEqual(self.events(), [])

    def test_sensor_command_is_logged_as_ignored_not_as_command(self):
        for cls in (sim.ContactSensor, sim.MotionSensor, sim.ClimateSensor):
            dev = self.make(cls, name=f"s_{cls.type}")
            self.send(dev, "set", b'{"state": "ON"}')
            self.send(dev, "get", b'{"state": ""}')
        time.sleep(0.2)
        names = [(e["direction"], e["event"]) for e in self.events()]
        self.assertEqual(names, [("down", "command_ignored")] * 6)
        self.assertEqual(self.client.messages, [], "czujnik nic nie publikuje po poleceniu")
        self.assertNotIn(("down", "command"), names)

    def test_routes_cover_set_set_state_and_get_including_slashed_names(self):
        a = self.plug(name="kitchen/plug")
        b = self.make(sim.ContactSensor, name="door")
        routes = sim.build_routes({a.name: a, b.name: b})
        base = sim.BASE
        self.assertEqual(routes[f"{base}/kitchen/plug/set"], (a, "set"))
        self.assertEqual(routes[f"{base}/kitchen/plug/set/state"], (a, "set_state"))
        self.assertEqual(routes[f"{base}/kitchen/plug/get"], (a, "get"))
        self.assertEqual(len(routes), 6)
        self.assertEqual(routes[f"{base}/door/get"], (b, "get"))


class AvailabilityAndBridgeTests(SimTestCase):
    def avail(self, dev):
        return [json.loads(m[2])["state"] for m in self.client.on_topic(f"{dev.topic}/availability")]

    def test_bridge_state_is_retained_json(self):
        sim.publish_bridge_state(self.client, "online")
        msg = self.client.on_topic(sim.BRIDGE_STATE_TOPIC)[0]
        self.assertEqual(json.loads(msg[2]), {"state": "online"})
        self.assertTrue(msg[3])

    def test_default_timeouts_differ_for_mains_and_battery(self):
        self.assertEqual(self.plug().avail_timeout, sim.AVAIL_ACTIVE_S)
        self.assertEqual(self.make(sim.ContactSensor, name="c").avail_timeout, sim.AVAIL_PASSIVE_S)
        self.assertLess(sim.AVAIL_ACTIVE_S, sim.AVAIL_PASSIVE_S)

    def test_device_goes_offline_after_timeout_and_returns_on_next_message(self):
        dev = self.plug(availability_timeout_s=0.2)
        dev.publish_discovery()
        self.assertEqual(self.avail(dev), ["online"])
        dev.check_availability(time.time())
        self.assertEqual(self.avail(dev), ["online"], "jeszcze w limicie")
        time.sleep(0.3)
        dev.check_availability(time.time())
        self.assertEqual(self.avail(dev), ["online", "offline"])
        dev.check_availability(time.time())
        self.assertEqual(self.avail(dev), ["online", "offline"], "bez powtórzeń")
        dev.emit("report")
        self.assertTrue(wait_for(lambda: self.avail(dev) == ["online", "offline", "online"]))
        names = [e[1] for e in self.event_names()]
        self.assertIn("availability_offline", names)
        self.assertIn("availability_online", names)

    def test_lost_messages_do_not_refresh_availability(self):
        dev = self.plug(availability_timeout_s=0.15, loss=1.0)
        dev.publish_discovery()
        for _ in range(3):
            dev.emit("report")
        time.sleep(0.25)
        dev.check_availability(time.time())
        self.assertEqual(self.avail(dev), ["online", "offline"])

    def test_zero_timeout_disables_availability_check(self):
        dev = self.plug(availability_timeout_s=0)
        dev.publish_discovery()
        dev.last_seen -= 10 ** 6
        dev.check_availability(time.time())
        self.assertEqual(self.avail(dev), ["online"])

    def test_discovery_availability_uses_bridge_state_json(self):
        dev = self.make(sim.ClimateSensor, name="clim")
        entity = next(iter(dev.entities()))[2]
        topics = {a["topic"]: a.get("value_template") for a in entity["availability"]}
        self.assertEqual(topics[sim.BRIDGE_STATE_TOPIC], "{{ value_json.state }}")
        self.assertEqual(topics[f"{dev.topic}/availability"], "{{ value_json.state }}")
        self.assertEqual(entity["availability_mode"], "all")

    def test_optimistic_publication_does_not_refresh_last_seen(self):
        dev = self.plug(optimistic=True, loss_down=1.0, availability_timeout_s=0.15)
        dev.publish_discovery()
        time.sleep(0.2)
        self.send(dev, "set", b'{"state": "ON"}')  # stan z bramki nie dowodzi, że urządzenie żyje
        dev.check_availability(time.time())
        self.assertEqual(self.avail(dev), ["online", "offline"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
