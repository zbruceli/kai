// The Stick's own wake-up alarm, as pure logic (no Arduino), so it can be unit-tested on the host:
//   pio test -e native
//
// The relay is the only source of truth for what's due. The Stick keeps just one absolute time, "wake me
// then", taken from the latest `schedule` message, and a timer wake only makes it reconnect and ask.
// The alarm lives in RTC memory and is measured on the RTC clock, which keeps running through deep sleep:
// a wake that can't reach the relay must not lose it, or a reminder would wait for the next button press.
#pragma once
#include <stdint.h>

namespace wakealarm {

constexpr int64_t SECOND_US = 1000000;
constexpr int64_t MIN_SLEEP_US = 30 * SECOND_US;       // never wake more often than this
constexpr int64_t RETRY_US = 5 * 60 * SECOND_US;       // alarm passed without reaching the relay: try again
constexpr uint32_t MAX_WAKE_IN_S = 7 * 24 * 3600;      // ignore anything further out than a week

// A `schedule` message: wake in `wakeInS` seconds, or 0 for no timer. Returns the new absolute alarm
// (0 = none). A fresh schedule always replaces the old alarm: it reflects the relay's current state.
inline int64_t alarmFromSchedule(uint32_t wakeInS, int64_t nowUs) {
  if (wakeInS == 0) return 0;
  if (wakeInS > MAX_WAKE_IN_S) wakeInS = MAX_WAKE_IN_S;
  return nowUs + int64_t(wakeInS) * SECOND_US;
}

// How long to deep-sleep before the timer should wake the Stick, or -1 for no timer wake.
// An alarm already in the past means this wake didn't get a fresh schedule (Wi-Fi or the relay was
// unreachable), so the relay never confirmed it: retry later rather than drop it.
inline int64_t sleepUs(int64_t alarmAtUs, int64_t nowUs) {
  if (alarmAtUs <= 0) return -1;
  int64_t remain = alarmAtUs - nowUs;
  if (remain <= 0) remain = RETRY_US;
  return remain < MIN_SLEEP_US ? MIN_SLEEP_US : remain;
}

}  // namespace wakealarm
