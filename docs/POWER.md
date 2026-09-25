# Kai power budget

The battery is 250 mAh; about **225 mAh** is usable down to the power chip's cutoff. The PMIC (M5PM1)
reports battery voltage but not current, so the per-state numbers below are **estimates** built from
M5Stack's measurements and component datasheets. The measurement plan at the end replaces them with
real numbers.

## Power rails (StickS3)

| Rail | Powers | Switched by |
|---|---|---|
| L0 | M5PM1 only | always on (with battery) |
| L1 | BMI270 IMU | PM1 LDO |
| L2 / L3A | ESP32-S3, buttons, IR, Grove/Hat | PM1 (L2 while the ESP32 sleeps, L3A while it runs) |
| L3B | LCD and backlight, mic, ES8311 codec, speaker | PM1 GPIO2 (the amp also has PM1 GPIO3) |

M5Stack's own measurements at 4.2 V: off 14 µA · L1 52 µA · L2 (ESP32 deep sleep) 102 µA ·
L3A (ESP32 running) 36.7 mA · full load 519 mA.

## Current draw by state (estimated, at the battery)

| State | CPU and system | Display | Wi-Fi | Audio | IMU | **Total** |
|---|---|---|---|---|---|---|
| Idle face, backlight 90 | 37 | 3 + 7 | +15 (modem sleep) | 4 (amp enabled) | 1 | **~67 mA** |
| Idle, dimmed (15) | 37 | 3 + 1 | +15 | 4 | 1 | **~61 mA** |
| Listening | 37 | 10 | +100 (awake, streaming up) | 5 (mic, codec) | 1 | **~153 mA** |
| Thinking | 37 | 10 | +85 (awake) | 4 | 1 | **~137 mA** |
| Speaking | 37 | 10 | +85 | ~105 (codec, amp, speech) | 1 | **~238 mA** |
| Reply on screen, silent | 37 | 10 | +85 (still awake) | 4 | 1 | **~137 mA** |
| Deep sleep, as built | 0.1 | ~0.1 (L3B left on) | 0 | ~1 (codec powered) | 0.9 | **~2 mA** |
| Deep sleep, L3B off, IMU suspended | | | | | | **~0.1 mA** (M5's L2 figure) |
| PM1 power-off (button wake) | | | | | | **0.014 mA** |

Measured software load, idle on the face: the main loop spins about **300 times a second** and spends
**50% of its time redrawing** (a full 240×135 frame at 30 fps). The CPU never idles at 240 MHz, and the
37 mA baseline assumes exactly that.

## Battery life on a full charge

| Pattern | Average | Runtime |
|---|---|---|
| Awake on the idle face, nonstop | 67 mA | **3.4 h** |
| Talking nonstop | ~200 mA | **1.1 h** |
| Deep sleep only (as built) | 2 mA | **~4.7 days** |
| Deep sleep only, L3B off | 0.1 mA | months (self-discharge dominates) |

**One question, with the current timers** (6 s listening, 3 s thinking, 10 s speaking, then the idle
tail until deep sleep):

| Phase | Time | Charge |
|---|---|---|
| Listen, think, speak | 19 s | 3.7 As (16%) |
| Reply stays on screen, Wi-Fi awake | 60 s | 8.2 As (35%) |
| Idle face | 60 s | 4.0 As (17%) |
| Dimmed, then deep sleep | 120 s | 7.3 As (31%) |
| Wake and reconnect | 3 s | 0.5 As (2%) |
| **Total** | ~4.4 min | **~6.6 mAh** |

**Over 80% of each question's energy goes to the tail after the answer**, not to the conversation.

**A day:**
- 20 questions: 132 mAh + 22.5 h of deep sleep at 2 mA (45 mAh) = 177 mAh/day, so **~1.3 days per charge**.
- 10 questions: ~113 mAh/day, so **~2 days per charge**.

## Firmware 0.8: power savings

The 0.7 budget above showed the idle tail and a CPU that never rested as the main drains. 0.8 targets
them:

| Change | Why |
|---|---|
| Redraw only on change: 20 fps while audio animates, 4 fps otherwise (was 30 fps always) | drawing time 50% → 8% (measured) |
| `delay()` in the loop so FreeRTOS idles the CPU | loop rate 294/s → 75/s (measured) |
| 80 MHz CPU unless listening, thinking or speaking | about -15 mA idle |
| Wi-Fi `MAX_MODEM` power save whenever no audio is streaming, including the reply screen after speech | the radio naps between beacons |
| Amp **and** ES8311 codec off except while speaking or listening (M5Unified left the DAC running) | -4 to 5 mA idle |
| 5V boost off at boot (M5Unified left it running) and IMU asleep | ~-1 to 2 mA always |
| Deep sleep cuts the L3B rail (LCD, backlight, codec, amp), verified by reading PM1 back | ~2 mA → ~0.15 mA |
| Timers: reply screen 60 → 20 s, dim 60 → 15 s, deep sleep 3 min → 45 s | shorter tail |

**Projected, until measured:**

| State | 0.7 | 0.8 |
|---|---|---|
| Idle face | ~67 mA | ~28 mA |
| Reply screen after the answer | ~137 mA | ~28 mA |
| Deep sleep | ~2 mA | ~0.15 mA |
| Energy per question | 6.6 mAh | **~1.6 mAh** (talking is now 64% of it) |
| 20 questions a day | ~1.3 days | **~6 days** |
| 10 questions a day | ~2 days | **~11 days** |
| Idle face, nonstop | 3.4 h | ~8 h |

**Remaining levers:**
- **Speech volume:** the amp is now the largest share of each question.
- **Faster wakes:** a static IP would skip DHCP.
- **Near-zero sleep:** a PM1 power-off with IMU motion wake would draw 14–52 µA and give "pick it up to wake".

## Measuring it

Every 30 s the firmware reports battery mV, mode, backlight, and Wi-Fi power save to the relay
(`relay/data/power.csv`). Each wake reports the voltage and time across the deep sleep. `kai-power`
fits voltage slopes over steady stretches and converts them to mA through a LiPo curve (roughly ±20%
over 20+ minute windows). Run it on the relay server: `cd ~/kai/relay && uv run kai-power`.

- **T1, deep sleep (overnight):** charge to full, unplug, and leave Kai alone; it deep-sleeps after
  3 minutes. In the morning, press the side button. The relay log prints `woke after … h asleep … (~x mA)`.
- **T2, awake idle:** flash the hold-state build (`pio run -e sticks3_powertest -t upload`), unplug,
  and leave it on the idle face for 45 minutes. Then plug in and run `kai-power`.
- **T3, real use:** normal firmware, full charge, normal use until it dies. The telemetry gives
  runtime and the time spent in each state.

Voltage readings on USB power reflect the charger, not the battery, so only unplugged stretches count.
