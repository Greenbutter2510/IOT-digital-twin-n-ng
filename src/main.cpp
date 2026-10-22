#include <Arduino.h>
#include <ArduinoJson.h>
#include <BH1750.h>
#include <Adafruit_INA219.h>
#include <PubSubClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <Wire.h>
#include <mbedtls/md.h>
#include <sys/time.h>

#include "config.h"
#include "control_logic.h"

namespace {

constexpr uint8_t kLampCount = 3;
constexpr uint8_t kSdaPin = 21;
constexpr uint8_t kSclPin = 22;
constexpr uint8_t kBh1750Address = 0x23;
constexpr uint8_t kInaAddresses[kLampCount] = {0x40, 0x41, 0x44};
constexpr uint8_t kLedPins[kLampCount] = {25, 26, 27};
// Dung cac GPIO thuong cho nut nhan. Tranh GPIO16/RX2 vi UART cua Wokwi co
// the giu chan nay o trang thai khong mong muon, lam Lamp 03 khong tat duoc.
constexpr uint8_t kButtonPins[kLampCount] = {32, 33, 18};
constexpr unsigned long kTelemetryPeriodMs = 5000;
constexpr unsigned long kMqttRetryMs = 5000;
#ifdef IOT_SIMULATION
constexpr unsigned long kDebounceMs = 5;
#else
constexpr unsigned long kDebounceMs = 25;
#endif

#ifdef IOT_SIMULATION
constexpr uint8_t kSimLuxPin = 34;
constexpr uint8_t kSimCurrentPins[kLampCount] = {35, 36, 39};
#endif

static_assert(inaAddressesAreUnique(kInaAddresses[0], kInaAddresses[1],
                                    kInaAddresses[2]),
              "Moi INA219 phai co dia chi I2C rieng");

struct Lamp {
  const char *deviceId;
  const char *secret;
  uint8_t ledPin;
  uint8_t buttonPin;
  bool on;
  bool automatic;
  bool sensorValid;
  float busVoltageV;
  float currentMa;
  uint32_t sequence;
  String lastCommandId;
  bool rawButton;
  bool stableButton;
  unsigned long buttonChangedAt;
};

Lamp lamps[kLampCount] = {
    {"lamp-01", IOT_HMAC_LAMP_01, kLedPins[0], kButtonPins[0], false, true,
     false, NAN, NAN, 1, "", false, false, 0},
    {"lamp-02", IOT_HMAC_LAMP_02, kLedPins[1], kButtonPins[1], false, true,
     false, NAN, NAN, 1, "", false, false, 0},
    {"lamp-03", IOT_HMAC_LAMP_03, kLedPins[2], kButtonPins[2], false, true,
     false, NAN, NAN, 1, "", false, false, 0},
};

BH1750 lightMeter;
Adafruit_INA219 ina1(kInaAddresses[0]);
Adafruit_INA219 ina2(kInaAddresses[1]);
Adafruit_INA219 ina3(kInaAddresses[2]);
Adafruit_INA219 *inaSensors[kLampCount] = {&ina1, &ina2, &ina3};
bool luxSensorPresent = false;
float sharedLux = NAN;
bool sharedLuxValid = false;
int autoOffLux = kDefaultAutoOffLux;
Preferences preferences;
String lastGatewayCommandId;
String lastGatewayAction;
String appliedGatewayCommandId;

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
unsigned long lastTelemetryAt = 0;
unsigned long lastMqttAttemptAt = 0;
unsigned long lastWifiAttemptAt = 0;

unsigned long long epochMs() {
  timeval tv{};
  gettimeofday(&tv, nullptr);
  return static_cast<unsigned long long>(tv.tv_sec) * 1000ULL +
         static_cast<unsigned long long>(tv.tv_usec) / 1000ULL;
}

String hmacSha256(const String &message, const char *secret) {
  byte result[32];
  mbedtls_md_context_t context;
  mbedtls_md_init(&context);
  const mbedtls_md_info_t *info =
      mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  mbedtls_md_setup(&context, info, 1);
  mbedtls_md_hmac_starts(&context,
                         reinterpret_cast<const unsigned char *>(secret),
                         strlen(secret));
  mbedtls_md_hmac_update(
      &context, reinterpret_cast<const unsigned char *>(message.c_str()),
      message.length());
  mbedtls_md_hmac_finish(&context, result);
  mbedtls_md_free(&context);

  String digest;
  digest.reserve(64);
  char pair[3];
  for (byte value : result) {
    snprintf(pair, sizeof(pair), "%02x", value);
    digest += pair;
  }
  return digest;
}

bool constantTimeEquals(const String &left, const String &right) {
  if (left.length() != right.length()) return false;
  uint8_t difference = 0;
  for (size_t i = 0; i < left.length(); ++i) {
    difference |= static_cast<uint8_t>(left[i] ^ right[i]);
  }
  return difference == 0;
}

String commandSigningText(const JsonDocument &doc) {
  String text;
  text.reserve(180);
  text += doc["commandId"].as<String>();
  text += '|';
  text += doc["deviceId"].as<String>();
  text += '|';
  text += doc["issuedAt"].as<String>();
  text += '|';
  text += doc["expiresAt"].as<String>();
  text += '|';
  text += doc["mode"].as<String>();
  text += '|';
  text += doc["lamp"].as<String>();
  return text;
}

void applyOutput(Lamp &lamp) {
  digitalWrite(lamp.ledPin, lamp.on ? HIGH : LOW);
}

void applyAutoDecision(Lamp &lamp) {
  lamp.on = nextLampState(sharedLux, sharedLuxValid, lamp.automatic, lamp.on,
                          autoOffLux);
  applyOutput(lamp);
}

void publishSignedState(Lamp &lamp, const char *status, const char *reason) {
  StaticJsonDocument<512> doc;
  doc["deviceId"] = lamp.deviceId;
  doc["commandId"] = lamp.lastCommandId;
  doc["status"] = status;
  doc["reason"] = reason;
  doc["lampState"] = lamp.on ? "ON" : "OFF";
  doc["brightness"] = lamp.on ? 100 : 0;
  doc["mode"] = lamp.automatic ? "AUTO" : "MANUAL";
  doc["timestamp"] = epochMs();
  String unsignedPayload;
  serializeJson(doc, unsignedPayload);
  doc["signature"] = hmacSha256(unsignedPayload, lamp.secret);
  String payload;
  serializeJson(doc, payload);
  String topic = "city/streetlight/" + String(lamp.deviceId) + "/state";
  mqtt.publish(topic.c_str(), payload.c_str(), false);
}

String gatewaySigningText(const JsonDocument &doc) {
  String text;
  text.reserve(180);
  text += doc["commandId"].as<String>();
  text += '|';
  text += doc["deviceId"].as<String>();
  text += '|';
  text += doc["issuedAt"].as<String>();
  text += '|';
  text += doc["expiresAt"].as<String>();
  text += '|';
  text += doc["scope"].as<String>();
  text += '|';
  text += doc["action"].as<String>();
  text += '|';
  text += doc["threshold"].as<String>();
  return text;
}

void publishGatewayState(const char *status, const char *reason) {
  StaticJsonDocument<512> doc;
  doc["deviceId"] = "gateway";
  doc["commandId"] = lastGatewayCommandId;
  doc["status"] = status;
  doc["reason"] = reason;
  doc["scope"] = "gateway";
  doc["action"] = lastGatewayAction;
  doc["autoOffLux"] = autoOffLux;
  bool allAuto = true;
  bool allManual = true;
  for (Lamp &lamp : lamps) {
    allAuto &= lamp.automatic;
    allManual &= !lamp.automatic;
  }
  doc["mode"] = allAuto ? "AUTO" : allManual ? "MANUAL" : "MIXED";
  doc["timestamp"] = epochMs();
  String unsignedPayload;
  serializeJson(doc, unsignedPayload);
  doc["signature"] = hmacSha256(unsignedPayload, lamps[0].secret);
  String payload;
  serializeJson(doc, payload);
  mqtt.publish("city/streetlight/gateway/state", payload.c_str(), false);
}

void handleGatewayCommand(byte *payload, unsigned int length) {
  String message;
  message.reserve(length);
  for (unsigned int i = 0; i < length; ++i) message += char(payload[i]);
  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, message)) return;
  const String commandId = doc["commandId"] | "";
  const String deviceId = doc["deviceId"] | "";
  const String scope = doc["scope"] | "";
  const String action = doc["action"] | "";
  const String signature = doc["signature"] | "";
  const uint64_t issuedAt = doc["issuedAt"] | 0ULL;
  const uint64_t expiresAt = doc["expiresAt"] | 0ULL;
  const uint64_t now = epochMs();
  const bool thresholdType = doc["threshold"].is<int>();
  const int threshold = thresholdType ? doc["threshold"].as<int>() : -1;
  const bool validAction = action == "AUTO_ON" || action == "AUTO_OFF" ||
                           action == "SET_THRESHOLD";
  const bool validThreshold = thresholdType && validAutoOffLux(threshold);
  const bool validShape = commandId.length() >= 8 && deviceId == "gateway" &&
                          scope == "ALL" && validAction && validThreshold;
  const String expected = hmacSha256(gatewaySigningText(doc), lamps[0].secret);
  if (!validShape || !constantTimeEquals(signature, expected)) {
    Serial.println("[gateway] Tu choi lenh sai fields/HMAC");
    return;
  }
  lastGatewayCommandId = commandId;
  lastGatewayAction = action;
  if (now <= 1000000000000ULL || issuedAt > now + 5000ULL ||
      expiresAt < now || expiresAt < issuedAt ||
      appliedGatewayCommandId == commandId) {
    publishGatewayState("REJECTED", "EXPIRED_OR_REPLAYED");
    return;
  }
  if (action != "AUTO_OFF" && threshold != autoOffLux &&
      preferences.putInt("autoOffLux", threshold) != sizeof(int)) {
    publishGatewayState("REJECTED", "NVS_WRITE_FAILED");
    return;
  }
  appliedGatewayCommandId = commandId;
  if (action == "SET_THRESHOLD") {
    autoOffLux = threshold;
    for (Lamp &lamp : lamps) {
      if (lamp.automatic) applyAutoDecision(lamp);
    }
  } else if (action == "AUTO_ON") {
    autoOffLux = threshold;
    for (Lamp &lamp : lamps) lamp.automatic = true;
    for (Lamp &lamp : lamps) applyAutoDecision(lamp);
  } else {
    for (Lamp &lamp : lamps) lamp.automatic = false;
  }
  publishGatewayState("APPLIED", "OK");
  // PubSubClient dung chung mot buffer cho callback va publish. De telemetry
  // sang loop ke tiep, tranh ghi de payload command khi callback chua ket thuc.
  lastTelemetryAt = millis() - kTelemetryPeriodMs;
}

int findLampFromTopic(const String &topic) {
  for (uint8_t i = 0; i < kLampCount; ++i) {
    String expected = "city/streetlight/" + String(lamps[i].deviceId) +
                      "/command";
    if (topic == expected) return i;
  }
  return -1;
}

void mqttCallback(char *topic, byte *payload, unsigned int length) {
  if (String(topic) == "city/streetlight/gateway/command") {
    handleGatewayCommand(payload, length);
    return;
  }
  const int index = findLampFromTopic(String(topic));
  if (index < 0) return;
  Lamp &lamp = lamps[index];

  String message;
  message.reserve(length);
  for (unsigned int i = 0; i < length; ++i) message += char(payload[i]);

  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, message)) {
    Serial.printf("[%s] Tu choi lenh: JSON khong hop le\n", lamp.deviceId);
    return;
  }

  const String commandId = doc["commandId"] | "";
  const String deviceId = doc["deviceId"] | "";
  const String mode = doc["mode"] | "";
  const String requestedState = doc["lamp"] | "HOLD";
  const String receivedSignature = doc["signature"] | "";
  const uint64_t issuedAt = doc["issuedAt"] | 0ULL;
  const uint64_t expiresAt = doc["expiresAt"] | 0ULL;
  const String expectedSignature = hmacSha256(commandSigningText(doc), lamp.secret);

  const uint64_t now = epochMs();
  const bool timeValid = now > 1000000000000ULL;
  const bool validShape = commandId.length() >= 8 && deviceId == lamp.deviceId &&
                          mode == "MANUAL" &&
                          (requestedState == "ON" || requestedState == "OFF");
  const bool validTime = timeValid && issuedAt <= now + 5000ULL &&
                         expiresAt >= now && expiresAt >= issuedAt;
  // So sanh replay voi ID da ap dung truoc khi gan ID moi.
  static String appliedIds[kLampCount];
  const bool newCommand = appliedIds[index] != commandId;
  // Lệnh chưa qua HMAC không được tạo ACK gắn với commandId do kẻ tấn công chọn.
  if (!validShape ||
      !constantTimeEquals(receivedSignature, expectedSignature)) {
    const char *reason = !validShape ? "INVALID_FIELDS" : "INVALID_HMAC";
    Serial.printf("[%s] Tu choi lenh: %s\n", lamp.deviceId, reason);
    return;
  }
  lamp.lastCommandId = commandId;
  if (!validTime || !newCommand) {
    const char *reason = !validTime ? "EXPIRED_OR_CLOCK_INVALID"
                                    : "REPLAYED_COMMAND";
    Serial.printf("[%s] Tu choi lenh da ky: %s\n", lamp.deviceId, reason);
    publishSignedState(lamp, "REJECTED", reason);
    return;
  }

  bool allManual = true;
  for (const Lamp &other : lamps) allManual &= !other.automatic;
  if (!allManual) {
    publishSignedState(lamp, "REJECTED", "USE_GATEWAY_OR_MANUAL_MODE");
    return;
  }
  appliedIds[index] = commandId;
  enterManualMode(requestedState == "ON", lamp.automatic, lamp.on);
  applyOutput(lamp);
  Serial.printf("[%s] Ap dung lenh %s: %s/%s\n", lamp.deviceId,
                commandId.c_str(), mode.c_str(),
                lamp.on ? "ON" : "OFF");
  publishSignedState(lamp, "APPLIED", "OK");
}

bool scanAddress(uint8_t address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}

void scanI2cBus() {
  Serial.println("[I2C] Bat dau quet bus...");
  uint8_t count = 0;
  for (uint8_t address = 1; address < 127; ++address) {
    if (scanAddress(address)) {
      Serial.printf("[I2C] Tim thay 0x%02X\n", address);
      ++count;
    }
  }
  Serial.printf("[I2C] Tong so thiet bi: %u\n", count);
}

void beginSensors() {
#ifdef IOT_SIMULATION
  pinMode(kSimLuxPin, INPUT);
  for (uint8_t pin : kSimCurrentPins) pinMode(pin, INPUT);
  luxSensorPresent = true;
  for (Lamp &lamp : lamps) lamp.sensorValid = true;
  Serial.println("[SIM] LDR/3 bien tro la adapter mo phong cho BH1750/INA219.");
#else
  Wire.begin(kSdaPin, kSclPin);
  Serial.printf("[I2C] Cau hinh BH1750=0x%02X, INA219=0x%02X/0x%02X/0x%02X (UNIQUE)\n",
                kBh1750Address, kInaAddresses[0], kInaAddresses[1],
                kInaAddresses[2]);
  scanI2cBus();
  luxSensorPresent = scanAddress(kBh1750Address) &&
                     lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE,
                                      kBh1750Address, &Wire);
  for (uint8_t i = 0; i < kLampCount; ++i) {
    lamps[i].sensorValid = scanAddress(kInaAddresses[i]) &&
                           inaSensors[i]->begin(&Wire);
    Serial.printf("[I2C] %s INA219 0x%02X: %s\n", lamps[i].deviceId,
                  kInaAddresses[i], lamps[i].sensorValid ? "OK" : "THIEU");
  }
  Serial.printf("[I2C] BH1750 0x%02X: %s\n", kBh1750Address,
                luxSensorPresent ? "OK" : "THIEU");
#endif
}

void readSensorsOnce() {
#ifdef IOT_SIMULATION
  const int rawLux = analogRead(kSimLuxPin);
  sharedLux = 1000.0f - (rawLux * 1000.0f / 4095.0f);
  sharedLuxValid = true;
  for (uint8_t i = 0; i < kLampCount; ++i) {
    lamps[i].currentMa =
        analogRead(kSimCurrentPins[i]) * 500.0f / 4095.0f;
    lamps[i].busVoltageV = lamps[i].on ? 5.0f : 0.0f;
    lamps[i].sensorValid = true;
  }
#else
  sharedLux = luxSensorPresent ? lightMeter.readLightLevel() : NAN;
  sharedLuxValid = luxSensorPresent && isfinite(sharedLux) && sharedLux >= 0.0f;
  for (uint8_t i = 0; i < kLampCount; ++i) {
    if (!lamps[i].sensorValid) {
      lamps[i].busVoltageV = NAN;
      lamps[i].currentMa = NAN;
      continue;
    }
    lamps[i].busVoltageV = inaSensors[i]->getBusVoltage_V();
    lamps[i].currentMa = inaSensors[i]->getCurrent_mA();
  }
#endif
}

void publishTelemetry(Lamp &lamp) {
  StaticJsonDocument<768> doc;
  doc["deviceId"] = lamp.deviceId;
  doc["timestamp"] = epochMs();
  doc["sequence"] = lamp.sequence;
  if (sharedLuxValid)
    doc["lux"] = serialized(String(sharedLux, 2));
  else
    doc["lux"] = nullptr;
  doc["luxValid"] = sharedLuxValid;
  if (lamp.sensorValid && isfinite(lamp.busVoltageV))
    doc["voltageV"] = serialized(String(lamp.busVoltageV, 3));
  else
    doc["voltageV"] = nullptr;
  if (lamp.sensorValid && isfinite(lamp.currentMa))
    doc["currentMa"] = serialized(String(lamp.currentMa, 2));
  else
    doc["currentMa"] = nullptr;
  doc["sensorValid"] = lamp.sensorValid;
  doc["lampState"] = lamp.on ? "ON" : "OFF";
  doc["brightness"] = lamp.on ? 100 : 0;
  doc["mode"] = lamp.automatic ? "AUTO" : "MANUAL";
  doc["autoOffLux"] = autoOffLux;
  doc["wifiRssi"] = WiFi.RSSI();
  doc["uptimeSec"] = millis() / 1000;

  String unsignedPayload;
  serializeJson(doc, unsignedPayload);
  doc["signature"] = hmacSha256(unsignedPayload, lamp.secret);
  String payload;
  serializeJson(doc, payload);
  String topic = "city/streetlight/" + String(lamp.deviceId) + "/telemetry";
  if (mqtt.publish(topic.c_str(), payload.c_str(), false)) {
    Serial.printf("[%s] Telemetry seq=%lu lux=%s state=%s\n", lamp.deviceId,
                  static_cast<unsigned long>(lamp.sequence),
                  sharedLuxValid ? String(sharedLux, 1).c_str() : "INVALID",
                  lamp.on ? "ON" : "OFF");
    ++lamp.sequence;
  } else {
    Serial.printf("[%s] Loi publish telemetry\n", lamp.deviceId);
  }
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(IOT_WIFI_SSID, IOT_WIFI_PASSWORD);
  Serial.print("[WiFi] Dang ket noi");
  const unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 15000) {
    Serial.print('.');
    delay(500);
  }
  if (WiFi.status() == WL_CONNECTED)
    Serial.printf("\n[WiFi] Da ket noi, IP=%s\n", WiFi.localIP().toString().c_str());
  else
    Serial.println("\n[WiFi] Timeout; dieu khien cuc bo van hoat dong");
}

void syncClock() {
  if (WiFi.status() != WL_CONNECTED) return;
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  const unsigned long started = millis();
  while (time(nullptr) < 100000 && millis() - started < 10000) delay(250);
  Serial.println(time(nullptr) >= 100000 ? "[NTP] Dong bo thanh cong"
                                         : "[NTP] Timeout; lenh tu xa se bi tu choi");
}

void connectMqtt() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (mqtt.connected() || millis() - lastMqttAttemptAt < kMqttRetryMs) return;
  lastMqttAttemptAt = millis();
  const String clientId = "streetlight-gateway-" +
                          String(static_cast<uint32_t>(ESP.getEfuseMac()), HEX);
  const bool connected = strlen(IOT_MQTT_USERNAME) > 0
                             ? mqtt.connect(clientId.c_str(), IOT_MQTT_USERNAME,
                                            IOT_MQTT_PASSWORD)
                             : mqtt.connect(clientId.c_str());
  if (!connected) {
    Serial.printf("[MQTT] Ket noi that bai rc=%d; thu lai sau 5s\n", mqtt.state());
    return;
  }
  Serial.printf("[MQTT] Da ket noi %s:%d\n", IOT_MQTT_HOST, IOT_MQTT_PORT);
  mqtt.subscribe("city/streetlight/gateway/command", 1);
  for (Lamp &lamp : lamps) {
    String topic = "city/streetlight/" + String(lamp.deviceId) + "/command";
    mqtt.subscribe(topic.c_str(), 1);
    String availability = "city/streetlight/" + String(lamp.deviceId) +
                          "/availability";
    mqtt.publish(availability.c_str(), "online", true);
  }
}

void handleButtons() {
  for (Lamp &lamp : lamps) {
    const bool raw = digitalRead(lamp.buttonPin) == LOW;
    if (raw != lamp.rawButton) {
      lamp.rawButton = raw;
      lamp.buttonChangedAt = millis();
    }
    if (millis() - lamp.buttonChangedAt >= kDebounceMs &&
        lamp.stableButton != raw) {
      lamp.stableButton = raw;
      if (raw) {
        enterManualMode(!lamp.on, lamp.automatic, lamp.on);
        applyOutput(lamp);
        Serial.printf("[%s] Nut local: %s\n", lamp.deviceId,
                      lamp.on ? "ON" : "OFF");
        lamp.lastCommandId = "local-button";
        publishSignedState(lamp, "APPLIED", "LOCAL_BUTTON");
      }
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[System] Khoi dong gateway 3 den duong");
  preferences.begin("streetlights", false);
  const int storedThreshold = preferences.getInt("autoOffLux", kDefaultAutoOffLux);
  autoOffLux = validAutoOffLux(storedThreshold) ? storedThreshold : kDefaultAutoOffLux;
  for (Lamp &lamp : lamps) {
    pinMode(lamp.ledPin, OUTPUT);
    pinMode(lamp.buttonPin, INPUT_PULLUP);
    applyOutput(lamp);
  }
  beginSensors();
  connectWifi();
  syncClock();
  const uint32_t seed = static_cast<uint32_t>(time(nullptr));
  for (Lamp &lamp : lamps) lamp.sequence = seed;
  mqtt.setBufferSize(1200);
  mqtt.setServer(IOT_MQTT_HOST, IOT_MQTT_PORT);
  mqtt.setCallback(mqttCallback);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED && millis() - lastWifiAttemptAt >= 10000) {
    lastWifiAttemptAt = millis();
    WiFi.reconnect();
  }
  connectMqtt();
  if (mqtt.connected()) mqtt.loop();
  handleButtons();

  if (millis() - lastTelemetryAt >= kTelemetryPeriodMs) {
    lastTelemetryAt = millis();
    readSensorsOnce();  // BH1750 dung chung chi duoc doc mot lan moi chu ky.
    for (Lamp &lamp : lamps) {
      applyAutoDecision(lamp);
      if (mqtt.connected()) publishTelemetry(lamp);
    }
  }
}
