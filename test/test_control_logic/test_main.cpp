#include <unity.h>

#include "control_logic.h"

extern "C" {
void setUp(void) {}
void tearDown(void) {}
}

void test_hysteresis_boundaries() {
  TEST_ASSERT_EQUAL_INT(static_cast<int>(AutoDecision::TurnOn),
                        static_cast<int>(decideAutoState(99.0f, true, false, 150)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(AutoDecision::Hold),
                        static_cast<int>(decideAutoState(100.0f, true, false, 150)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(AutoDecision::Hold),
                        static_cast<int>(decideAutoState(150.0f, true, true, 150)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(AutoDecision::TurnOff),
                        static_cast<int>(decideAutoState(151.0f, true, true, 150)));
}

void test_missing_lux_holds_state() {
  TEST_ASSERT_EQUAL_INT(static_cast<int>(AutoDecision::Hold),
                        static_cast<int>(decideAutoState(0.0f, false, false, 150)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(AutoDecision::Hold),
                        static_cast<int>(decideAutoState(999.0f, false, true, 150)));
}

void test_manual_override_and_return_auto() {
  bool automatic = true;
  bool on = false;
  enterManualMode(true, automatic, on);
  TEST_ASSERT_FALSE(automatic);
  TEST_ASSERT_TRUE(on);
  on = nextLampState(999.0f, true, automatic, on, 150);
  TEST_ASSERT_TRUE(on);  // MANUAL khong bi nguong AUTO ghi de.
  returnToAutoMode(automatic);
  TEST_ASSERT_TRUE(automatic);
  on = nextLampState(151.0f, true, automatic, on, 150);
  TEST_ASSERT_FALSE(on);
}

void test_three_lamps_are_independent() {
  bool automatic[3] = {false, true, false};
  bool on[3] = {false, false, true};
  enterManualMode(true, automatic[0], on[0]);
  on[1] = nextLampState(99.0f, true, automatic[1], on[1], 150);
  enterManualMode(false, automatic[2], on[2]);
  TEST_ASSERT_TRUE(on[0]);
  TEST_ASSERT_TRUE(on[1]);
  TEST_ASSERT_FALSE(on[2]);
}

void test_configurable_threshold_and_global_mode() {
  TEST_ASSERT_TRUE(validAutoOffLux(150));
  TEST_ASSERT_TRUE(validAutoOffLux(1000));
  TEST_ASSERT_FALSE(validAutoOffLux(155));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(AutoDecision::Hold),
                        static_cast<int>(decideAutoState(250.0f, true, false, 300)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(AutoDecision::TurnOn),
                        static_cast<int>(decideAutoState(249.0f, true, false, 300)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(AutoDecision::Hold),
                        static_cast<int>(decideAutoState(300.0f, true, true, 300)));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(AutoDecision::TurnOff),
                        static_cast<int>(decideAutoState(301.0f, true, true, 300)));
  bool automatic[3] = {false, true, false};
  bool on[3] = {true, false, true};
  for (int i = 0; i < 3; ++i) automatic[i] = true;
  for (int i = 0; i < 3; ++i) on[i] = nextLampState(301, true, automatic[i], on[i], 300);
  for (int i = 0; i < 3; ++i) TEST_ASSERT_FALSE(on[i]);
  for (int i = 0; i < 3; ++i) automatic[i] = false;
  for (int i = 0; i < 3; ++i) TEST_ASSERT_FALSE(on[i]);
}

void test_ina219_addresses_are_unique() {
  TEST_ASSERT_TRUE(inaAddressesAreUnique(0x40, 0x41, 0x44));
  TEST_ASSERT_FALSE(inaAddressesAreUnique(0x40, 0x40, 0x44));
}

int main(int argc, char **argv) {
  UNITY_BEGIN();
  RUN_TEST(test_hysteresis_boundaries);
  RUN_TEST(test_missing_lux_holds_state);
  RUN_TEST(test_manual_override_and_return_auto);
  RUN_TEST(test_three_lamps_are_independent);
  RUN_TEST(test_configurable_threshold_and_global_mode);
  RUN_TEST(test_ina219_addresses_are_unique);
  return UNITY_END();
}
