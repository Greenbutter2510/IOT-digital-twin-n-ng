import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest

import digital_twin_server as server


class Result:
    rc = 0


class FakeMqtt:
    def __init__(self):
        self.published = []
        self.subscriptions = []
        self.on_connect = None
        self.on_message = None

    def reconnect_delay_set(self, **kwargs):
        pass

    def username_pw_set(self, *args):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return Result()

    def subscribe(self, topics):
        self.subscriptions.append(topics)


class FakeThingsBoard:
    def __init__(self):
        self.published = []

    def publish(self, payload):
        self.published.append(payload)

    def stop(self):
        pass


def signed_payload(data, secret):
    unsigned = server.compact_json(data)
    signed = dict(data)
    signed["signature"] = hmac.new(
        secret.encode(), unsigned.encode(), hashlib.sha256
    ).hexdigest()
    return server.compact_json(signed)


class DigitalTwinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp.close()
        self.secrets = {
            "lamp-01": "test-secret-01",
            "lamp-02": "test-secret-02",
            "lamp-03": "test-secret-03",
        }
        server.SECRET_KEYS.update(self.secrets)
        self.mqtt = FakeMqtt()
        self.tb = FakeThingsBoard()
        self.twin = server.DigitalTwin(self.temp.name, self.mqtt, self.tb)

    def tearDown(self):
        os.unlink(self.temp.name)

    def telemetry(self, device_id, sequence, timestamp, **changes):
        data = {
            "deviceId": device_id,
            "timestamp": timestamp,
            "sequence": sequence,
            "lux": 90.0,
            "luxValid": True,
            "voltageV": 5.0,
            "currentMa": 120.0,
            "sensorValid": True,
            "lampState": "ON",
            "brightness": 100,
            "mode": "AUTO",
            "autoOffLux": 150,
            "wifiRssi": -45,
            "uptimeSec": 20,
        }
        data.update(changes)
        return signed_payload(data, self.secrets[device_id])

    def make_manual(self):
        now = int(time.time() * 1000)
        for device_id in server.DEVICE_IDS:
            self.twin.device_states[device_id] = {
                "mode": "MANUAL", "timestamp": now, "receivedAt": now,
                "autoOffLux": 150, "lampState": "OFF", "currentMa": 0.0,
                "voltageV": 0.0, "sensorValid": True,
            }

    def test_three_lamps_are_aggregated_before_thingsboard_publish(self):
        base = int(time.time() * 1000)
        for device_id in server.DEVICE_IDS:
            self.twin.handle_availability(device_id, "online")
        self.assertTrue(self.twin.handle_telemetry("lamp-01", self.telemetry("lamp-01", 1, base)))
        self.assertTrue(self.twin.handle_telemetry("lamp-02", self.telemetry("lamp-02", 1, base + 1)))
        self.assertEqual([], self.tb.published)
        self.assertTrue(self.twin.handle_telemetry("lamp-03", self.telemetry("lamp-03", 1, base + 2)))
        self.assertEqual(1, len(self.tb.published))
        values = self.tb.published[0]["values"]
        self.assertEqual("ON", values["lamp01_state"])
        self.assertIn("lamp03_sourceTs", values)

    def test_bad_hmac_and_replay_never_reach_thingsboard(self):
        base = int(time.time() * 1000)
        for device_id in server.DEVICE_IDS:
            self.assertTrue(self.twin.handle_telemetry(device_id, self.telemetry(device_id, 10, base)))
        baseline = len(self.tb.published)
        bad = json.loads(self.telemetry("lamp-01", 11, base + 1))
        bad["currentMa"] = 999
        self.assertFalse(self.twin.handle_telemetry("lamp-01", server.compact_json(bad)))
        self.assertFalse(self.twin.handle_telemetry("lamp-01", self.telemetry("lamp-01", 10, base)))
        self.assertEqual(baseline, len(self.tb.published))

    def test_sequence_reset_after_reboot_requires_newer_timestamp(self):
        base = int(time.time() * 1000)
        self.assertTrue(
            self.twin.handle_telemetry(
                "lamp-01", self.telemetry("lamp-01", 1_800_000_000, base)
            )
        )
        self.assertTrue(
            self.twin.handle_telemetry(
                "lamp-01", self.telemetry("lamp-01", 100, base + 5000)
            )
        )
        self.assertFalse(
            self.twin.handle_telemetry(
                "lamp-01", self.telemetry("lamp-01", 99, base + 5000)
            )
        )

    def test_missing_sensor_values_require_invalid_flags(self):
        payload = self.telemetry(
            "lamp-01", 1, int(time.time() * 1000),
            lux=None, luxValid=False, voltageV=None, currentMa=None, sensorValid=False,
        )
        self.assertTrue(self.twin.handle_telemetry("lamp-01", payload))
        invalid = self.telemetry(
            "lamp-02", 1, int(time.time() * 1000),
            voltageV=None, currentMa=None, sensorValid=True,
        )
        self.assertFalse(self.twin.handle_telemetry("lamp-02", invalid))

    def test_signed_command_fake_ack_and_replay_ack(self):
        self.make_manual()
        command_id = self.twin.send_command("lamp-01", "ON", "MANUAL")
        topic, encoded, _, _ = self.mqtt.published[-1]
        self.assertEqual("city/streetlight/lamp-01/command", topic)
        command = json.loads(encoded)
        unsigned = {key: value for key, value in command.items() if key != "signature"}
        expected = server.sign_command(unsigned, self.secrets["lamp-01"])["signature"]
        self.assertTrue(hmac.compare_digest(expected, command["signature"]))
        forged_command = dict(command)
        forged_command["lamp"] = "OFF"
        self.assertFalse(server.verify_command_signature(forged_command, self.secrets["lamp-01"]))

        fake_ack = server.compact_json(
            {"deviceId": "lamp-01", "commandId": command_id,
             "status": "APPLIED", "signature": "0" * 64}
        )
        self.assertFalse(self.twin.handle_state("lamp-01", fake_ack))
        ack = {
            "deviceId": "lamp-01", "commandId": command_id, "status": "APPLIED",
            "reason": "OK", "lampState": "ON", "brightness": 100,
            "mode": "MANUAL", "timestamp": command["issuedAt"] + 1,
        }
        encoded_ack = signed_payload(ack, self.secrets["lamp-01"])
        self.assertTrue(self.twin.handle_state("lamp-01", encoded_ack))
        self.assertFalse(self.twin.handle_state("lamp-01", encoded_ack))

    def test_gateway_auto_command_preserves_online_state(self):
        self.twin.db.update_device("lamp-03", availability="online")
        command_id = self.twin.send_gateway_command("AUTO_ON")
        command = json.loads(self.twin.db.get_command(command_id)["payload"])
        self.assertEqual(("AUTO_ON", "ALL"), (command["action"], command["scope"]))
        self.assertEqual(server.TOPIC_GATEWAY_COMMAND, self.mqtt.published[-2][0])
        with self.twin.db.session() as connection:
            availability = connection.execute(
                "SELECT availability FROM devices WHERE device_id='lamp-03'"
            ).fetchone()[0]
        self.assertEqual("online", availability)

    def test_gateway_threshold_validation_signature_and_ack(self):
        for value in (99, 155, 1001, True, "200", float("nan")):
            self.assertIsNone(self.twin.handle_dashboard_command(
                json.dumps({"scope": "gateway", "action": "SET_THRESHOLD", "threshold": value})
            ))
        command_id = self.twin.send_gateway_command("SET_THRESHOLD", 300)
        topic, encoded, _, _ = self.mqtt.published[-2]
        self.assertEqual(server.TOPIC_GATEWAY_COMMAND, topic)
        command = json.loads(encoded)
        self.assertEqual(300, command["threshold"])
        tampered = dict(command, threshold=400)
        self.assertNotEqual(command["signature"], server.sign_gateway_command(
            {key: value for key, value in tampered.items() if key != "signature"},
            self.secrets["lamp-01"]
        )["signature"])
        ack = {"deviceId": "gateway", "commandId": command_id,
               "status": "APPLIED", "reason": "OK", "scope": "gateway",
               "action": "SET_THRESHOLD", "autoOffLux": 300,
               "mode": "MIXED", "timestamp": command["issuedAt"] + 1}
        self.assertFalse(self.twin.handle_gateway_state(server.compact_json(ack)))
        signed_ack = signed_payload(ack, self.secrets["lamp-01"])
        self.assertTrue(self.twin.handle_gateway_state(signed_ack))
        self.assertFalse(self.twin.handle_gateway_state(signed_ack))
        self.assertEqual("APPLIED", self.twin.db.get_command(command_id)["status"])

    def test_manual_rejected_until_all_report_manual(self):
        self.assertIsNone(self.twin.handle_dashboard_command(
            json.dumps({"deviceId": "lamp-01", "mode": "MANUAL", "lamp": "ON"})
        ))
        self.make_manual()
        self.assertIsNotNone(self.twin.handle_dashboard_command(
            json.dumps({"deviceId": "lamp-01", "mode": "MANUAL", "lamp": "ON"})
        ))

    def test_expired_ack_is_rejected(self):
        self.make_manual()
        command_id = self.twin.send_command("lamp-02", "OFF", "MANUAL")
        command = json.loads(self.twin.db.get_command(command_id)["payload"])
        ack = {
            "deviceId": "lamp-02", "commandId": command_id, "status": "APPLIED",
            "reason": "OK", "lampState": "OFF", "brightness": 0,
            "mode": "MANUAL", "timestamp": command["expiresAt"] + 1,
        }
        self.assertFalse(
            self.twin.handle_state(
                "lamp-02", signed_payload(ack, self.secrets["lamp-02"])
            )
        )
        self.assertEqual("EXPIRED", self.twin.db.get_command(command_id)["status"])

    def test_local_button_updates_state_immediately(self):
        self.make_manual()
        ack = {
            "deviceId": "lamp-01", "commandId": "local-button",
            "status": "APPLIED", "reason": "LOCAL_BUTTON",
            "lampState": "ON", "brightness": 100,
            "mode": "MANUAL", "timestamp": int(time.time() * 1000),
        }
        encoded = signed_payload(ack, self.secrets["lamp-01"])
        self.assertTrue(self.twin.handle_state("lamp-01", encoded))
        self.assertEqual("ON", self.twin.device_states["lamp-01"]["lampState"])
        self.assertEqual("MANUAL", self.twin.device_states["lamp-01"]["mode"])
        # Fake HMAC must be rejected
        fake = signed_payload(ack, "wrong-secret")
        self.assertFalse(self.twin.handle_state("lamp-01", fake))


if __name__ == "__main__":
    unittest.main()
