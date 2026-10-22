#pragma once

// Dat gia tri that trong include/secrets.h (tep nay da duoc .gitignore).
// Xem include/secrets.example.h. Build Wokwi nap gia tri demo tu platformio.ini.
#if __has_include("secrets.h")
#include "secrets.h"
#endif

#ifndef IOT_WIFI_SSID
#define IOT_WIFI_SSID "CHANGE_ME"
#endif
#ifndef IOT_WIFI_PASSWORD
#define IOT_WIFI_PASSWORD "CHANGE_ME"
#endif
#ifndef IOT_MQTT_HOST
#define IOT_MQTT_HOST "192.0.2.1"
#endif
#ifndef IOT_MQTT_PORT
#define IOT_MQTT_PORT 1883
#endif
#ifndef IOT_MQTT_USERNAME
#define IOT_MQTT_USERNAME ""
#endif
#ifndef IOT_MQTT_PASSWORD
#define IOT_MQTT_PASSWORD ""
#endif
#ifndef IOT_HMAC_LAMP_01
#define IOT_HMAC_LAMP_01 "CHANGE_ME_LAMP_01"
#endif
#ifndef IOT_HMAC_LAMP_02
#define IOT_HMAC_LAMP_02 "CHANGE_ME_LAMP_02"
#endif
#ifndef IOT_HMAC_LAMP_03
#define IOT_HMAC_LAMP_03 "CHANGE_ME_LAMP_03"
#endif
