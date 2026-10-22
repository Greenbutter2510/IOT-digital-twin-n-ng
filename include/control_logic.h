#pragma once

enum class AutoDecision { Hold, TurnOn, TurnOff };

constexpr int kDefaultAutoOffLux = 150;
constexpr int kAutoHysteresisLux = 50;
constexpr int kMinAutoOffLux = 100;
constexpr int kMaxAutoOffLux = 1000;

constexpr bool validAutoOffLux(int threshold) {
  return threshold >= kMinAutoOffLux && threshold <= kMaxAutoOffLux &&
         threshold % 10 == 0;
}

constexpr AutoDecision decideAutoState(float lux, bool luxValid, bool isOn,
                                       int autoOffLux) {
  return !luxValid || !validAutoOffLux(autoOffLux)
             ? AutoDecision::Hold
         : (lux < autoOffLux - kAutoHysteresisLux && !isOn)
             ? AutoDecision::TurnOn
         : (lux > autoOffLux && isOn) ? AutoDecision::TurnOff
                                      : AutoDecision::Hold;
}

inline bool nextLampState(float lux, bool luxValid, bool automatic, bool isOn,
                          int autoOffLux) {
  if (!automatic) return isOn;
  const AutoDecision decision = decideAutoState(lux, luxValid, isOn, autoOffLux);
  if (decision == AutoDecision::TurnOn) return true;
  if (decision == AutoDecision::TurnOff) return false;
  return isOn;
}

constexpr bool inaAddressesAreUnique(unsigned char a, unsigned char b,
                                     unsigned char c) {
  return (a != b) && (a != c) && (b != c);
}

inline void enterManualMode(bool requestedOn, bool &automatic, bool &isOn) {
  automatic = false;
  isOn = requestedOn;
}

inline void returnToAutoMode(bool &automatic) { automatic = true; }
