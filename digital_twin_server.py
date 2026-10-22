"""Digital Twin gateway cho mot ESP32 va ba den duong.

Chi telemetry vuot qua HMAC + anti-replay moi duoc luu, phat cho Node-RED
va dua vao hang doi ThingsBoard. ThingsBoard la kenh telemetry mot chieu;
luong command/ACK noi bo van di qua Digital Twin.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import ssl
import threading
import time
import uuid
from typing import Any, Optional

import paho.mqtt.client as mqtt

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v


DEVICE_IDS = ("lamp-01", "lamp-02", "lamp-03")
SECRET_KEYS = {
    device_id: os.getenv(f"IOT_HMAC_LAMP_0{index}", "")
    for index, device_id in enumerate(DEVICE_IDS, start=1)
}

MQTT_BROKER = os.getenv("IOT_MQTT_BROKER", "127.0.0.1")
MQTT_PORT = int(os.getenv("IOT_MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("IOT_MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("IOT_MQTT_PASSWORD", "")

TOPIC_TELEMETRY = "city/streetlight/+/telemetry"
TOPIC_STATE = "city/streetlight/+/state"
TOPIC_COMMAND = "city/streetlight/{device_id}/command"
TOPIC_GATEWAY_COMMAND = "city/streetlight/gateway/command"
TOPIC_GATEWAY_STATE = "city/streetlight/gateway/state"
DEFAULT_AUTO_OFF_LUX = 150
TOPIC_DASHBOARD_COMMAND = "city/dashboard/command/request"
TOPIC_AVAILABILITY = "city/streetlight/+/availability"
TOPIC_ALERT = "city/streetlight/+/alert"
TOPIC_VERIFIED = "city/digital-twin/verified/telemetry"
STALE_AFTER_MS = int(os.getenv("IOT_STALE_AFTER_MS", "15000"))
MAX_CLOCK_SKEW_MS = int(os.getenv("IOT_MAX_CLOCK_SKEW_MS", "120000"))
SIMULATION_CLOCK = os.getenv("IOT_SIMULATION_CLOCK", "false").lower() in (
    "1", "true", "yes", "on"
)
VOLTAGE_MIN = os.getenv("IOT_VOLTAGE_MIN", "")
VOLTAGE_MAX = os.getenv("IOT_VOLTAGE_MAX", "")
OFF_LEAK_MA = float(os.getenv("IOT_OFF_LEAK_MA", "100"))
ON_MIN_MA = float(os.getenv("IOT_ON_MIN_MA", "10"))
MAX_CURRENT_MA = float(os.getenv("IOT_MAX_CURRENT_MA", "1000"))

SIGNATURE_RE = re.compile(
    r',\s*"signature"\s*:\s*"([a-fA-F0-9]{64})"\s*}\s*$'
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def command_signing_text(data: dict[str, Any]) -> str:
    fields = ("commandId", "deviceId", "issuedAt", "expiresAt", "mode", "lamp")
    return "|".join(str(data.get(field, "")) for field in fields)


def verify_trailing_signature(
    payload: str, secret: str
) -> tuple[bool, Optional[dict[str, Any]]]:
    """Xac minh JSON do ArduinoJson tao, trong do signature la truong cuoi."""
    if not secret:
        return False, None
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return False, None
    match = SIGNATURE_RE.search(payload)
    if not match:
        return False, parsed
    unsigned_payload = payload[: match.start()] + "}"
    expected = hmac.new(
        secret.encode("utf-8"), unsigned_payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, match.group(1).lower()), parsed


def sign_command(data: dict[str, Any], secret: str) -> dict[str, Any]:
    signed = dict(data)
    signed["signature"] = hmac.new(
        secret.encode("utf-8"),
        command_signing_text(data).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return signed


def gateway_signing_text(data: dict[str, Any]) -> str:
    fields = ("commandId", "deviceId", "issuedAt", "expiresAt", "scope", "action", "threshold")
    return "|".join(str(data.get(field, "")) for field in fields)


def sign_gateway_command(data: dict[str, Any], secret: str) -> dict[str, Any]:
    signed = dict(data)
    signed["signature"] = hmac.new(
        secret.encode("utf-8"), gateway_signing_text(data).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return signed


def valid_threshold(value: Any) -> bool:
    return type(value) is int and 100 <= value <= 1000 and value % 10 == 0


def verify_command_signature(data: dict[str, Any], secret: str) -> bool:
    received = str(data.get("signature", ""))
    expected = sign_command(
        {key: value for key, value in data.items() if key != "signature"}, secret
    )["signature"]
    return hmac.compare_digest(expected, received)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_telemetry(data: dict[str, Any], topic_device_id: str) -> Optional[str]:
    if data.get("deviceId") != topic_device_id:
        return "DEVICE_ID_MISMATCH"
    if topic_device_id not in DEVICE_IDS:
        return "UNKNOWN_DEVICE"
    if not isinstance(data.get("sequence"), int) or isinstance(data.get("sequence"), bool):
        return "INVALID_SEQUENCE"
    if not isinstance(data.get("timestamp"), int) or data["timestamp"] <= 0:
        return "INVALID_TIMESTAMP"
    if data.get("lampState") not in ("ON", "OFF"):
        return "INVALID_LAMP_STATE"
    if data.get("mode") not in ("AUTO", "MANUAL"):
        return "INVALID_MODE"
    if not isinstance(data.get("luxValid"), bool):
        return "INVALID_LUX_STATUS"
    if data["luxValid"] and not _is_number(data.get("lux")):
        return "INVALID_LUX"
    if not isinstance(data.get("sensorValid"), bool):
        return "INVALID_CURRENT_STATUS"
    if data["sensorValid"] and (
        not _is_number(data.get("voltageV"))
        or not _is_number(data.get("currentMa"))
    ):
        return "INVALID_ELECTRICAL_READING"
    if not valid_threshold(data.get("autoOffLux")):
        return "INVALID_AUTO_THRESHOLD"
    if data.get("brightness") not in (0, 100):
        return "INVALID_ON_OFF_LEVEL"
    return None


class DigitalTwinDB:
    def __init__(self, db_path: str = "digital_twin.db") -> None:
        self.db_path = db_path
        self.lock = threading.RLock()
        self._create_tables()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def session(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _create_tables(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS telemetry (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          device_id TEXT NOT NULL,
          timestamp INTEGER NOT NULL,
          sequence INTEGER NOT NULL,
          lux REAL, lux_valid INTEGER NOT NULL DEFAULT 0,
          voltage_v REAL, current_ma REAL,
          sensor_valid INTEGER NOT NULL DEFAULT 0,
          lamp_state TEXT NOT NULL, brightness INTEGER NOT NULL,
          mode TEXT NOT NULL, auto_off_lux INTEGER NOT NULL DEFAULT 150,
          wifi_rssi INTEGER, uptime_sec INTEGER,
          received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS commands (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          command_id TEXT UNIQUE NOT NULL,
          device_id TEXT NOT NULL,
          payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'PENDING',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          responded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS devices (
          device_id TEXT PRIMARY KEY,
          availability TEXT NOT NULL DEFAULT 'offline',
          desired_state TEXT, reported_state TEXT,
          last_seen_ms INTEGER, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS security_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_code TEXT NOT NULL, severity TEXT NOT NULL,
          device_id TEXT, details TEXT, raw_payload TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          alert_type TEXT NOT NULL, severity TEXT NOT NULL,
          device_id TEXT, message TEXT NOT NULL, raw_payload TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
        with self.lock, self.session() as connection:
            connection.executescript(schema)
            existing = {
                row["name"] for row in connection.execute("PRAGMA table_info(telemetry)")
            }
            migrations = {
                "lux_valid": "ALTER TABLE telemetry ADD COLUMN lux_valid INTEGER NOT NULL DEFAULT 0",
                "sensor_valid": "ALTER TABLE telemetry ADD COLUMN sensor_valid INTEGER NOT NULL DEFAULT 0",
                "auto_off_lux": "ALTER TABLE telemetry ADD COLUMN auto_off_lux INTEGER NOT NULL DEFAULT 150",
            }
            for column, statement in migrations.items():
                if column not in existing:
                    connection.execute(statement)

    def insert_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        values = (
            device_id,
            data["timestamp"],
            data["sequence"],
            data.get("lux"),
            int(data["luxValid"]),
            data.get("voltageV"),
            data.get("currentMa"),
            int(data["sensorValid"]),
            data["lampState"],
            data["brightness"],
            data["mode"],
            data["autoOffLux"],
            data.get("wifiRssi"),
            data.get("uptimeSec"),
        )
        with self.lock, self.session() as connection:
            connection.execute(
                """INSERT INTO telemetry
                (device_id,timestamp,sequence,lux,lux_valid,voltage_v,current_ma,
                 sensor_valid,lamp_state,brightness,mode,auto_off_lux,wifi_rssi,uptime_sec)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )

    def last_sequence_and_timestamp(self, device_id: str) -> tuple[Optional[int], Optional[int]]:
        with self.lock, self.session() as connection:
            row = connection.execute(
                "SELECT sequence,timestamp FROM telemetry WHERE device_id=? ORDER BY id DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            # Cac database tao boi ban cu khai bao timestamp la TEXT. SQLite
            # giu nguyen schema cu khi CREATE TABLE IF NOT EXISTS chay lai, nen
            # can chuan hoa kieu ngay tai bien doc de phep so sanh anti-replay
            # luon la int, ke ca khi nang cap tren database dang co du lieu.
            if not row:
                return None, None
            return int(row["sequence"]), int(row["timestamp"])

    def insert_security_event(
        self, code: str, severity: str, device_id: Optional[str], details: str,
        raw_payload: Optional[str] = None,
    ) -> None:
        with self.lock, self.session() as connection:
            connection.execute(
                "INSERT INTO security_events(event_code,severity,device_id,details,raw_payload) VALUES(?,?,?,?,?)",
                (code, severity, device_id, details, raw_payload),
            )

    def insert_alert(
        self, alert_type: str, severity: str, device_id: Optional[str], message: str,
        raw_payload: Optional[str] = None,
    ) -> None:
        with self.lock, self.session() as connection:
            connection.execute(
                "INSERT INTO alerts(alert_type,severity,device_id,message,raw_payload) VALUES(?,?,?,?,?)",
                (alert_type, severity, device_id, message, raw_payload),
            )

    def insert_command(self, command_id: str, device_id: str, payload: str) -> None:
        with self.lock, self.session() as connection:
            connection.execute(
                "INSERT INTO commands(command_id,device_id,payload) VALUES(?,?,?)",
                (command_id, device_id, payload),
            )

    def get_command(self, command_id: str) -> Optional[sqlite3.Row]:
        with self.lock, self.session() as connection:
            return connection.execute(
                "SELECT * FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()

    def update_command_status(self, command_id: str, status: str) -> None:
        with self.lock, self.session() as connection:
            connection.execute(
                "UPDATE commands SET status=?,responded_at=CURRENT_TIMESTAMP WHERE command_id=?",
                (status, command_id),
            )

    def pending_commands(self) -> list[sqlite3.Row]:
        with self.lock, self.session() as connection:
            return connection.execute(
                "SELECT * FROM commands WHERE status='PENDING'"
            ).fetchall()

    def update_device(
        self, device_id: str, *, availability: Optional[str] = None,
        desired: Optional[str] = None, reported: Optional[str] = None,
    ) -> None:
        """Chi cap nhat truong duoc truyen; omitted availability khong thanh offline."""
        now_ms = int(time.time() * 1000)
        with self.lock, self.session() as connection:
            current = connection.execute(
                "SELECT * FROM devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if current is None:
                connection.execute(
                    """INSERT INTO devices(device_id,availability,desired_state,
                    reported_state,last_seen_ms) VALUES(?,?,?,?,?)""",
                    (device_id, availability or "offline", desired, reported, now_ms),
                )
            else:
                connection.execute(
                    """UPDATE devices SET availability=?,desired_state=?,reported_state=?,
                    last_seen_ms=?,updated_at=CURRENT_TIMESTAMP WHERE device_id=?""",
                    (
                        current["availability"] if availability is None else availability,
                        current["desired_state"] if desired is None else desired,
                        current["reported_state"] if reported is None else reported,
                        now_ms,
                        device_id,
                    ),
                )


class ThingsBoardBridge:
    """Ket noi MQTT/TLS rieng, mot chieu, co hang doi huu han."""

    def __init__(self, logger: logging.Logger, enabled: Optional[bool] = None) -> None:
        self.logger = logger
        configured = os.getenv("TB_ENABLED", "false").lower() in ("1", "true", "yes")
        self.enabled = configured if enabled is None else enabled
        self.host = os.getenv("TB_HOST", "")
        self.port = int(os.getenv("TB_PORT", "8883"))
        self.token = os.getenv("TB_DEVICE_TOKEN", "")
        self.ca_cert = os.getenv("TB_CA_CERT", "")
        self.connected = False
        self.queue: deque[str] = deque(maxlen=int(os.getenv("TB_QUEUE_SIZE", "100")))
        self.client: Optional[mqtt.Client] = None

        if not self.enabled:
            self.logger.info("ThingsBoard dang tat (TB_ENABLED=false)")
            return
        if not self.host or not self.token:
            self.logger.error("ThingsBoard bi vo hieu: thieu TB_HOST hoac TB_DEVICE_TOKEN")
            self.enabled = False
            return

        self.client = mqtt.Client(client_id=f"digital-twin-tb-{uuid.uuid4().hex[:8]}")
        self.client.username_pw_set(self.token)
        context = ssl.create_default_context(cafile=self.ca_cert or None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.client.tls_set_context(context)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish
        self.client.connect_async(self.host, self.port, 60)
        self.client.loop_start()
        self.logger.info("Da khoi tao kenh ThingsBoard MQTT/TLS %s:%d", self.host, self.port)

    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        self.connected = rc == 0
        if not self.connected:
            self.logger.warning("ThingsBoard ket noi that bai rc=%s", rc)
            return
        self.logger.info("ThingsBoard da ket noi bang MQTT/TLS")
        while self.queue:
            client.publish("v1/devices/me/telemetry", self.queue.popleft(), qos=1)

    def _on_disconnect(self, client, userdata, rc, properties=None) -> None:
        self.connected = False
        if rc:
            self.logger.warning("ThingsBoard mat ket noi; du lieu se vao hang doi")

    def _on_publish(self, client, userdata, mid) -> None:
        self.logger.info("ThingsBoard da nhan telemetry (PUBACK mid=%s)", mid)

    def publish(self, payload: dict[str, Any]) -> None:
        if not self.enabled or self.client is None:
            return
        encoded = compact_json(payload)
        if self.connected:
            result = self.client.publish("v1/devices/me/telemetry", encoded, qos=1)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                return
        if len(self.queue) == self.queue.maxlen:
            self.logger.warning("Hang doi ThingsBoard day; bo ban ghi cu nhat")
        self.queue.append(encoded)

    def stop(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()


@dataclass
class PublishResult:
    rc: int = mqtt.MQTT_ERR_SUCCESS


class DigitalTwin:
    def __init__(
        self, db_path: str = "digital_twin.db", mqtt_client: Optional[mqtt.Client] = None,
        thingsboard: Optional[ThingsBoardBridge] = None,
    ) -> None:
        self.logger = logging.getLogger("DigitalTwin")
        self.db = DigitalTwinDB(db_path)
        self.device_states: dict[str, dict[str, Any]] = {}
        self.state_lock = threading.RLock()
        self.device_availability: dict[str, str] = {}
        self.gateway_pending: Optional[str] = None
        self.gateway_status = "IDLE"
        self.gateway_reported_mode: Optional[str] = None
        self.gateway_reported_threshold: Optional[int] = None
        self.gateway_reported_at: Optional[int] = None
        self.gateway_applied_deadline: Optional[int] = None
        for row in self.db.pending_commands():
            self.db.update_command_status(row["command_id"], "INTERRUPTED")
            if row["device_id"] == "gateway":
                self.gateway_status = "INTERRUPTED"
        self._expire_commands()
        self.mqtt_client = mqtt_client or mqtt.Client(client_id="digital-twin-server")
        self.thingsboard = thingsboard or ThingsBoardBridge(self.logger)
        self._setup_mqtt()

    def _setup_mqtt(self) -> None:
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
        if MQTT_USERNAME:
            self.mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    def connect(self) -> None:
        self.logger.info("Dang ket noi private MQTT broker %s:%d", MQTT_BROKER, MQTT_PORT)
        self.mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, 60)

    def on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        if rc != 0:
            self.logger.error("MQTT noi bo ket noi that bai rc=%s", rc)
            return
        client.subscribe(
            [(TOPIC_TELEMETRY, 1), (TOPIC_STATE, 1), (TOPIC_AVAILABILITY, 1),
             (TOPIC_ALERT, 1), (TOPIC_DASHBOARD_COMMAND, 1),
             (TOPIC_GATEWAY_STATE, 1)]
        )
        self.logger.info("DIGITAL TWIN SERVER DA KHOI DONG")

    def on_message(self, client, userdata, msg) -> None:
        with self.state_lock:
            self._on_message(client, userdata, msg)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            self._security("MALFORMED_UTF8", "HIGH", None, "Payload khong phai UTF-8")
            return
        if msg.topic == TOPIC_DASHBOARD_COMMAND:
            self.handle_dashboard_command(payload)
            return
        if msg.topic == TOPIC_GATEWAY_STATE:
            self.handle_gateway_state(payload)
            return
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[:2] != ["city", "streetlight"]:
            return
        device_id, suffix = parts[2], parts[3]
        if suffix == "telemetry":
            self.handle_telemetry(device_id, payload)
        elif suffix == "state":
            self.handle_state(device_id, payload)
        elif suffix == "availability":
            self.handle_availability(device_id, payload)
        elif suffix == "alert":
            self.db.insert_alert("DEVICE_ALERT", "MEDIUM", device_id, payload, payload)

    def _security(
        self, code: str, severity: str, device_id: Optional[str], details: str,
        raw_payload: Optional[str] = None,
    ) -> None:
        self.db.insert_security_event(code, severity, device_id, details, raw_payload)
        self.logger.warning("[%s] %s (%s)", code, details, device_id or "unknown")

    def handle_telemetry(self, device_id: str, payload: str) -> bool:
        valid_signature, data = verify_trailing_signature(payload, SECRET_KEYS.get(device_id, ""))
        if data is None:
            self._security("MALFORMED_JSON", "HIGH", device_id, "Telemetry JSON khong hop le", payload)
            return False
        error = validate_telemetry(data, device_id)
        if error:
            self._security(error, "HIGH", device_id, "Telemetry sai kieu/noi dung", payload)
            return False
        if not valid_signature:
            self._security("INVALID_HMAC", "CRITICAL", device_id, "Telemetry sai HMAC", payload)
            return False

        last_sequence, last_timestamp = self.db.last_sequence_and_timestamp(device_id)
        if last_timestamp is not None and data["timestamp"] <= last_timestamp:
            self._security("REPLAY_TIMESTAMP", "HIGH", device_id, "Timestamp quay lui", payload)
            return False
        if last_sequence is not None and data["sequence"] <= last_sequence:
            # Sequence duoc khoi tao lai khi ESP32 reboot. Chi chap nhan gia tri
            # thap hon neu timestamp da tien ve phia truoc; goi phat lai cu van
            # bi chan boi kiem tra timestamp o tren.
            self.logger.info(
                "Chap nhan sequence reset %s: %s -> %s",
                device_id, last_sequence, data["sequence"],
            )
        now_ms = int(time.time() * 1000)
        if not SIMULATION_CLOCK and abs(data["timestamp"] - now_ms) > MAX_CLOCK_SKEW_MS:
            self._security("CLOCK_SKEW", "HIGH", device_id, "Timestamp nam ngoai cua so cho phep", payload)
            return False

        self.db.insert_telemetry(device_id, data)
        state = dict(data)
        state.pop("signature", None)
        state["availability"] = "online"
        state["receivedAt"] = int(time.time() * 1000)
        self.device_states[device_id] = state
        self.device_availability[device_id] = "online"
        if self.gateway_applied_deadline and all(
            (self.gateway_reported_mode is None or
             self.device_states.get(item, {}).get("mode") == self.gateway_reported_mode) and
            self.device_states.get(item, {}).get("autoOffLux") == self.gateway_reported_threshold and
            self.device_states[item].get("receivedAt", 0) >= (self.gateway_reported_at or 0)
            for item in DEVICE_IDS
        ):
            self.gateway_reported_mode = None
            self.gateway_reported_threshold = None
            self.gateway_reported_at = None
            self.gateway_applied_deadline = None
        self.db.update_device(
            device_id, availability="online", reported=compact_json(state)
        )
        self._expire_commands()
        snapshot = self.build_snapshot()
        self.mqtt_client.publish(TOPIC_VERIFIED, compact_json(snapshot), qos=1, retain=True)
        if snapshot["complete"]:
            self.thingsboard.publish(self.to_thingsboard(snapshot))
        self._evaluate_alerts(device_id, data, payload)
        self.logger.info("Da xac minh telemetry %s seq=%s", device_id, data["sequence"])
        return True

    def _evaluate_alerts(self, device_id: str, data: dict[str, Any], payload: str) -> None:
        if data["sensorValid"]:
            voltage = float(data["voltageV"])
            current = float(data["currentMa"])
            if VOLTAGE_MIN and VOLTAGE_MAX and voltage > 0 and not (
                float(VOLTAGE_MIN) <= voltage <= float(VOLTAGE_MAX)
            ):
                self.db.insert_alert("AR-02", "MEDIUM", device_id, "Dien ap bat thuong", payload)
            if data["lampState"] == "OFF" and current > OFF_LEAK_MA:
                self.db.insert_alert("AR-03", "HIGH", device_id, "Co dong khi den OFF", payload)
            if data["lampState"] == "ON" and current < ON_MIN_MA:
                self.db.insert_alert("AR-04", "MEDIUM", device_id, "Den ON nhung dong rat thap", payload)
            if current > MAX_CURRENT_MA:
                self.db.insert_alert("AR-05", "CRITICAL", device_id, "Qua dong", payload)

    def build_snapshot(self) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        newest = max(
            self.device_states.values(), key=lambda state: state.get("timestamp", 0),
            default={},
        )
        lamps: dict[str, Any] = {}
        for device_id in DEVICE_IDS:
            state = self.device_states.get(device_id)
            if state is None:
                lamps[device_id] = {"available": False, "stale": True}
                continue
            source_ts = int(state["timestamp"])
            lamps[device_id] = {
                "available": self.device_availability.get(device_id, state.get("availability")) != "offline",
                "stale": now_ms - state.get("receivedAt", 0) > STALE_AFTER_MS,
                "sourceTs": source_ts,
                "state": state["lampState"],
                "mode": state["mode"],
                "currentMa": state.get("currentMa"),
                "voltageV": state.get("voltageV"),
                "sensorValid": state["sensorValid"],
                "autoOffLux": state["autoOffLux"],
            }
        reported_threshold = newest.get("autoOffLux", DEFAULT_AUTO_OFF_LUX)
        return {
            "verifiedAt": now_ms,
            "complete": all(device_id in self.device_states for device_id in DEVICE_IDS),
            "lux": newest.get("lux"),
            "luxValid": newest.get("luxValid", False),
            "autoOffLux": reported_threshold,
            "gatewayPending": self.gateway_pending is not None or self.gateway_applied_deadline is not None,
            "gatewayStatus": (
                "WAITING_TELEMETRY" if self.gateway_applied_deadline is not None
                else self.gateway_status
            ),
            "gatewayReportedMode": self.gateway_reported_mode,
            "gatewayReportedThreshold": self.gateway_reported_threshold,
            "lamps": lamps,
        }

    @staticmethod
    def to_thingsboard(snapshot: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {
            "lux": snapshot["lux"],
            "luxValid": snapshot["luxValid"],
            "autoOffLux": snapshot["autoOffLux"],
        }
        timestamps = []
        for index, device_id in enumerate(DEVICE_IDS, start=1):
            lamp = snapshot["lamps"][device_id]
            prefix = f"lamp0{index}"
            values.update(
                {
                    f"{prefix}_state": lamp["state"],
                    f"{prefix}_mode": lamp["mode"],
                    f"{prefix}_currentMa": lamp["currentMa"],
                    f"{prefix}_voltageV": lamp["voltageV"],
                    f"{prefix}_sensorValid": lamp["sensorValid"],
                    f"{prefix}_sourceTs": lamp["sourceTs"],
                    f"{prefix}_stale": lamp["stale"],
                }
            )
            timestamps.append(lamp["sourceTs"])
        return {"ts": max(timestamps), "values": values}

    def handle_dashboard_command(self, payload: str) -> Optional[str]:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("payload phai la object")
            if data.get("scope") == "gateway":
                return self.send_gateway_command(data.get("action"), data.get("threshold"))
            device_id = str(data.get("deviceId", ""))
            mode = str(data.get("mode", "MANUAL")).upper()
            lamp = str(data.get("lamp", "")).upper()
            return self.send_command(device_id, lamp, mode)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            self._security("INVALID_DASHBOARD_COMMAND", "HIGH", None, str(error), payload)
            if isinstance(locals().get("data"), dict) and data.get("scope") == "gateway":
                if self.gateway_pending is None and self.gateway_applied_deadline is None:
                    self.gateway_status = "REQUEST_REJECTED"
                    self.publish_snapshot()
            return None

    def send_gateway_command(self, action: Any, threshold: Any = None) -> str:
        if type(action) is not str or action not in ("AUTO_ON", "AUTO_OFF", "SET_THRESHOLD"):
            raise ValueError("gateway action khong hop le")
        if action == "SET_THRESHOLD":
            if not valid_threshold(threshold):
                raise ValueError("threshold phai la so nguyen 100..1000, buoc 10")
        else:
            threshold = self.build_snapshot()["autoOffLux"]
            if not valid_threshold(threshold):
                threshold = DEFAULT_AUTO_OFF_LUX
        self._expire_commands()
        if self.gateway_pending or self.gateway_applied_deadline is not None:
            raise ValueError("gateway dang cho ACK/telemetry")
        if not SECRET_KEYS.get("lamp-01"):
            raise ValueError("thieu secret gateway")
        now_ms = int(time.time() * 1000)
        if SIMULATION_CLOCK:
            state = self.device_states.get("lamp-01")
            if state and isinstance(state.get("timestamp"), int):
                now_ms = max(1, int(state["timestamp"]) - 1000)
        command = {
            "commandId": str(uuid.uuid4()), "deviceId": "gateway",
            "issuedAt": now_ms, "expiresAt": now_ms + 30000,
            "scope": "ALL", "action": action, "threshold": threshold,
        }
        encoded = compact_json(sign_gateway_command(command, SECRET_KEYS["lamp-01"]))
        self.db.insert_command(command["commandId"], "gateway", encoded)
        result = self.mqtt_client.publish(TOPIC_GATEWAY_COMMAND, encoded, qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            self.db.update_command_status(command["commandId"], "PUBLISH_FAILED")
            self.gateway_status = "PUBLISH_FAILED"
            self.publish_snapshot()
            raise ValueError("khong gui duoc gateway command")
        self.gateway_pending = command["commandId"]
        self.gateway_status = "PENDING"
        self.publish_snapshot()
        return command["commandId"]

    def _expire_commands(self) -> None:
        now_ms = int(time.time() * 1000)
        if self.gateway_applied_deadline and now_ms > self.gateway_applied_deadline:
            self.gateway_reported_mode = None
            self.gateway_reported_threshold = None
            self.gateway_reported_at = None
            self.gateway_applied_deadline = None
            self.gateway_status = "TELEMETRY_TIMEOUT"
        for row in self.db.pending_commands():
            original = json.loads(row["payload"])
            created_at = datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)
            deadline = (int(created_at.timestamp() * 1000) + 35000
                        if SIMULATION_CLOCK else original["expiresAt"] + 5000)
            if deadline < now_ms:
                self.db.update_command_status(row["command_id"], "TIMEOUT")
                if row["command_id"] == self.gateway_pending:
                    self.gateway_pending = None
                    self.gateway_status = "TIMEOUT"

    def publish_snapshot(self) -> None:
        self._expire_commands()
        self.mqtt_client.publish(
            TOPIC_VERIFIED, compact_json(self.build_snapshot()), qos=1, retain=True
        )

    def handle_gateway_state(self, payload: str) -> bool:
        valid_signature, data = verify_trailing_signature(payload, SECRET_KEYS.get("lamp-01", ""))
        if data is None or not valid_signature or data.get("deviceId") != "gateway":
            self._security("FAKE_GATEWAY_ACK", "CRITICAL", "gateway", "ACK gateway sai HMAC/JSON", payload)
            return False
        command_id = str(data.get("commandId", ""))
        command = self.db.get_command(command_id)
        if command is None or command["device_id"] != "gateway" or command["status"] != "PENDING":
            self._security("UNKNOWN_GATEWAY_ACK", "HIGH", "gateway", "ACK khong cho", payload)
            return False
        try:
            original = json.loads(command["payload"])
            now_ms = int(time.time() * 1000)
            timestamp = data["timestamp"]
            if type(timestamp) is not int or not original["issuedAt"] <= timestamp <= original["expiresAt"]:
                raise ValueError("timestamp ACK gateway sai")
            if SIMULATION_CLOCK:
                created_at = datetime.fromisoformat(command["created_at"]).replace(
                    tzinfo=timezone.utc
                )
                if now_ms > int(created_at.timestamp() * 1000) + 35000:
                    raise ValueError("ACK gateway qua han")
            elif now_ms > original["expiresAt"] + 5000:
                raise ValueError("ACK gateway qua han")
            if data.get("scope") != "gateway" or data.get("action") != original["action"]:
                raise ValueError("scope/action ACK gateway sai")
            if data.get("status") not in ("APPLIED", "REJECTED"):
                raise ValueError("status ACK gateway sai")
            if not valid_threshold(data.get("autoOffLux")):
                raise ValueError("nguong ACK gateway sai")
            if data["status"] == "APPLIED":
                expected = original["threshold"] if original["action"] != "AUTO_OFF" else None
                if expected is not None and data["autoOffLux"] != expected:
                    raise ValueError("nguong ACK khong khop")
                expected_mode = "AUTO" if original["action"] == "AUTO_ON" else "MANUAL" if original["action"] == "AUTO_OFF" else None
                if expected_mode and data.get("mode") != expected_mode:
                    raise ValueError("mode ACK khong khop")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._security("INVALID_GATEWAY_ACK", "HIGH", "gateway", str(error), payload)
            return False
        self.db.update_command_status(command_id, data["status"])
        if self.gateway_pending == command_id:
            self.gateway_pending = None
            self.gateway_status = data["status"]
            if data["status"] == "APPLIED":
                self.gateway_reported_mode = (
                    data.get("mode") if original["action"] != "SET_THRESHOLD" else None
                )
                self.gateway_reported_threshold = data["autoOffLux"]
                self.gateway_reported_at = now_ms
                self.gateway_applied_deadline = now_ms + STALE_AFTER_MS
        self.publish_snapshot()
        return True

    def send_command(self, device_id: str, lamp: str, mode: str = "MANUAL") -> str:
        mode, lamp = mode.upper(), lamp.upper()
        if device_id not in DEVICE_IDS or not SECRET_KEYS.get(device_id):
            raise ValueError("deviceId khong duoc cau hinh secret")
        if mode != "MANUAL" or lamp not in ("ON", "OFF"):
            raise ValueError("chi ho tro MANUAL ON/OFF")
        self._expire_commands()
        if self.gateway_status in ("TIMEOUT", "TELEMETRY_TIMEOUT", "REJECTED", "PUBLISH_FAILED", "INTERRUPTED"):
            raise ValueError("gateway chua duoc xac nhan; can dong bo lai AUTO chung")
        if self.gateway_pending or self.gateway_applied_deadline is not None or any(
            self.device_states.get(item, {}).get("mode") != "MANUAL" or
            self.device_availability.get(item) == "offline" or
            int(time.time() * 1000) - self.device_states[item].get("receivedAt", 0) > STALE_AFTER_MS
            for item in DEVICE_IDS
        ):
            raise ValueError("can ba den MANUAL va telemetry con moi")

        now_ms = int(time.time() * 1000)
        if SIMULATION_CLOCK:
            state = self.device_states.get(device_id)
            if state and isinstance(state.get("timestamp"), int):
                # Wokwi co the chay cham hon thoi gian thuc. Dung mien thoi
                # gian cua thiet bi de ESP32 khong tu choi command hop le.
                now_ms = max(1, int(state["timestamp"]) - 1000)
        command = {
            "commandId": str(uuid.uuid4()),
            "deviceId": device_id,
            "issuedAt": now_ms,
            "expiresAt": now_ms + 30000,
            "mode": mode,
            "lamp": lamp,
        }
        signed = sign_command(command, SECRET_KEYS[device_id])
        encoded = compact_json(signed)
        self.db.insert_command(command["commandId"], device_id, encoded)
        self.db.update_device(device_id, desired=encoded)
        result = self.mqtt_client.publish(
            TOPIC_COMMAND.format(device_id=device_id), encoded, qos=1
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            self.db.update_command_status(command["commandId"], "PUBLISH_FAILED")
            raise ValueError(f"Khong xep duoc command MQTT rc={result.rc}")
        self.logger.info("Da gui command %s cho %s", command["commandId"], device_id)
        return command["commandId"]

    def handle_state(self, device_id: str, payload: str) -> bool:
        valid_signature, data = verify_trailing_signature(payload, SECRET_KEYS.get(device_id, ""))
        if data is None:
            self._security("MALFORMED_ACK", "HIGH", device_id, "ACK JSON khong hop le", payload)
            return False
        if not valid_signature or data.get("deviceId") != device_id:
            self._security("FAKE_ACK", "CRITICAL", device_id, "ACK sai HMAC/deviceId", payload)
            return False
        command_id = data.get("commandId")
        if command_id == "local-button":
            if data.get("status") == "APPLIED" and data.get("reason") == "LOCAL_BUTTON":
                state = self.device_states.setdefault(device_id, {})
                state["lampState"] = data.get("lampState", "OFF")
                state["mode"] = data.get("mode", "MANUAL")
                state["brightness"] = data.get("brightness", 0)
                state["timestamp"] = data.get("timestamp")
                state["receivedAt"] = int(time.time() * 1000)
                self.publish_snapshot()
                self.logger.info("[%s] Cap nhat trang thai tu nut bam cuc bo: %s (%s)", device_id, state["lampState"], state["mode"])
                return True
            return False
        command = self.db.get_command(str(command_id))
        if command is None or command["device_id"] != device_id:
            self._security("UNKNOWN_ACK", "CRITICAL", device_id, "ACK co commandId la", payload)
            return False
        if command["status"] != "PENDING":
            self._security("REPLAY_ACK", "HIGH", device_id, "ACK bi phat lai", payload)
            return False
        if data.get("status") not in ("APPLIED", "REJECTED"):
            self._security("INVALID_ACK_STATUS", "HIGH", device_id, "ACK status sai", payload)
            return False
        try:
            original = json.loads(command["payload"])
            ack_timestamp = int(data["timestamp"])
            now_ms = int(time.time() * 1000)
            if not original["issuedAt"] <= ack_timestamp <= original["expiresAt"]:
                raise ValueError("ACK timestamp nam ngoai thoi han command")
            if SIMULATION_CLOCK:
                created_at = datetime.fromisoformat(command["created_at"]).replace(
                    tzinfo=timezone.utc
                )
                if now_ms > int(created_at.timestamp() * 1000) + 35000:
                    raise ValueError("ACK den sau cua so 5 giay")
            elif now_ms > original["expiresAt"] + 5000:
                raise ValueError("ACK den sau cua so 5 giay")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.db.update_command_status(command_id, "EXPIRED")
            self._security("EXPIRED_ACK", "HIGH", device_id, str(error), payload)
            return False
        self.db.update_command_status(command_id, data["status"])
        self.db.update_device(device_id, reported=compact_json(data))
        self.publish_snapshot()
        self.logger.info("ACK %s cho command %s", data["status"], command_id)
        return True

    def handle_availability(self, device_id: str, payload: str) -> None:
        if device_id not in DEVICE_IDS or payload not in ("online", "offline"):
            self._security("INVALID_AVAILABILITY", "MEDIUM", device_id, payload)
            return
        self.db.update_device(device_id, availability=payload)
        self.device_availability[device_id] = payload
        if device_id in self.device_states:
            self.device_states[device_id]["availability"] = payload
        self.publish_snapshot()

    def run(self, console: bool = True) -> None:
        self.connect()
        self.mqtt_client.loop_start()
        stop_updates = threading.Event()

        def refresh_snapshot() -> None:
            while not stop_updates.wait(5):
                with self.state_lock:
                    self.publish_snapshot()

        refresh_thread = threading.Thread(target=refresh_snapshot, daemon=True)
        refresh_thread.start()
        try:
            if not console:
                while True:
                    time.sleep(1)
            while console:
                command = input("> ").strip()
                if command == "quit":
                    break
                if command == "status":
                    print(json.dumps(self.build_snapshot(), ensure_ascii=False, indent=2))
                elif command.startswith("send "):
                    parts = command.split()
                    try:
                        mode = parts[3] if len(parts) > 3 else "MANUAL"
                        print(self.send_command(parts[1], parts[2], mode))
                    except (IndexError, ValueError) as error:
                        print(f"Loi: {error}")
                elif command:
                    print("Lenh: status | send <lamp-01..03> <ON/OFF/HOLD> [MANUAL/AUTO] | quit")
        except KeyboardInterrupt:
            pass
        finally:
            stop_updates.set()
            refresh_thread.join(timeout=2)
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.thingsboard.stop()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    DigitalTwin().run()
