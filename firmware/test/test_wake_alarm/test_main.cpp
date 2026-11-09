// Host-side tests for the Stick's wake alarm (src/wake_alarm.h): pio test -e native
#include <unity.h>

#include "wake_alarm.h"

using namespace wakealarm;

static constexpr int64_t S = SECOND_US;
static constexpr int64_t T0 = 1000 * S;  // some RTC time after boot

void setUp() {}
void tearDown() {}

void test_no_schedule_means_no_timer() {
  TEST_ASSERT_EQUAL_INT64(0, alarmFromSchedule(0, T0));
  TEST_ASSERT_EQUAL_INT64(-1, sleepUs(0, T0));
}

void test_sleeps_until_the_alarm() {
  const int64_t alarm = alarmFromSchedule(180, T0);  // "remind me in 3 minutes"
  TEST_ASSERT_EQUAL_INT64(T0 + 180 * S, alarm);
  TEST_ASSERT_EQUAL_INT64(135 * S, sleepUs(alarm, T0 + 45 * S));  // asleep after 45 s idle
}

void test_a_fresh_schedule_replaces_the_alarm() {
  int64_t alarm = alarmFromSchedule(180, T0);
  alarm = alarmFromSchedule(600, T0 + 10 * S);  // moved later while connected
  TEST_ASSERT_EQUAL_INT64(T0 + 610 * S, alarm);
  alarm = alarmFromSchedule(0, T0 + 20 * S);    // cancelled: nothing left to wake for
  TEST_ASSERT_EQUAL_INT64(-1, sleepUs(alarm, T0 + 65 * S));
}

// The bug this fixes: a timer wake that can't reach the relay gets no fresh schedule. The alarm used to
// live in RAM and was lost on that wake, so the reminder waited for a button press. Now it's retried.
void test_offline_timer_wake_keeps_and_retries_the_alarm() {
  const int64_t alarm = alarmFromSchedule(180, T0);
  const int64_t wokeAt = T0 + 180 * S;  // timer fired; Wi-Fi down, no schedule this wake
  TEST_ASSERT_EQUAL_INT64(RETRY_US, sleepUs(alarm, wokeAt + 25 * S));
}

void test_offline_button_wake_keeps_a_future_alarm() {
  const int64_t alarm = alarmFromSchedule(2 * 3600, T0);  // a reminder in 2 h
  const int64_t now = T0 + 3600 * S;                      // a button wake an hour later, offline
  TEST_ASSERT_EQUAL_INT64(3600 * S, sleepUs(alarm, now));
}

void test_never_wakes_sooner_than_the_floor() {
  const int64_t alarm = alarmFromSchedule(40, T0);
  TEST_ASSERT_EQUAL_INT64(MIN_SLEEP_US, sleepUs(alarm, T0 + 35 * S));  // 5 s left -> 30 s floor
}

void test_far_schedules_are_capped_at_a_week() {
  TEST_ASSERT_EQUAL_INT64(T0 + int64_t(MAX_WAKE_IN_S) * S, alarmFromSchedule(100 * 24 * 3600, T0));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_no_schedule_means_no_timer);
  RUN_TEST(test_sleeps_until_the_alarm);
  RUN_TEST(test_a_fresh_schedule_replaces_the_alarm);
  RUN_TEST(test_offline_timer_wake_keeps_and_retries_the_alarm);
  RUN_TEST(test_offline_button_wake_keeps_a_future_alarm);
  RUN_TEST(test_never_wakes_sooner_than_the_floor);
  RUN_TEST(test_far_schedules_are_capped_at_a_week);
  return UNITY_END();
}
