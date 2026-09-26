# Changelog

## Unreleased

- Opus voice between the Stick and the relay: 16 kHz mic at ~24 kbit/s (was 256) and 24 kHz speech at
  ~32 kbit/s (was 384), about 11–12x less Wi-Fi airtime. On the Stick at 240 MHz, encoding takes ~6.4 ms
  and decoding ~3.9 ms per 20 ms frame (measured). Older firmware and `kai-sim` keep raw PCM.
- Measurement builds log Opus encode/decode time per frame (`enc_us`, `dec_us`).
- The sleeping z's are drawn as proper Z shapes (they read as "I").
- README: animated hero of Kai's moods and a gallery of reply screens.

## v0.8.1

- On USB power the screen now turns off after 45 s idle (was 4 minutes), matching battery timing. The Stick
  stays connected on USB, so it still answers instantly.

## v0.8.0: power

Battery life, projected from the power budget in docs/POWER.md (measurements pending): **~6 days at 20
questions a day**, up from ~1.3.

- Screen redraws only when something changes: drawing time 50% → 8%, main-loop rate 294 → 75 per
  second (measured).
- CPU drops to 80 MHz and Wi-Fi uses maximum power saving whenever no audio is streaming.
- The speaker amp and ES8311 codec power up only to speak or listen.
- The 5V boost is switched off; M5Unified had left it running. The IMU sleeps.
- Deep sleep cuts the LCD/audio power rail (PM1 GPIO2).
- Shorter wait after each answer: reply screen 20 s, dim at 15 s, deep sleep at 45 s on battery.
- Power budget and measurement protocol: `docs/POWER.md`. Measurement firmware builds
  (`sticks3_telemetry`, `sticks3_powertest`) plus `kai-power` estimate current from battery voltage.
  Release builds send no telemetry.

## v0.7.0: first release

Kai, a pocket AI pal on an M5StickS3, with a home relay that talks to Gemini Live.

**Talking**
- Push-to-talk voice with Gemini Live (`gemini-3.8-live`) and Google Search.
- Barge-in: pressing again interrupts Kai, and a quick tap silences it.
- Every answer is both spoken and shown on screen: a card plus a live transcript, scrollable with the
  side button.

**Tools**
- Weather: temperature first, plus high/low, rain, wind, waves, and sun times.
- NOAA tides for a single day.
- Golden hour and blue hour.
- Parking nearby.
- Trip notes, mirrored to Markdown.
- Days are understood as spoken ("tomorrow", "saturday"). Places resolve to the match nearest home.
- If Gemini answers without calling a tool, the relay runs it anyway so a card always appears.

**Look and power**
- Original Tamagotchi-style pixel Kai (pastel on plum), with idle, listening, thinking, speaking,
  happy, sleeping, and offline animations.
- Louder speaker: full volume on USB, plus a clip-safe digital boost.
- Dims after 1 minute. On battery it deep-sleeps after 3 minutes and wakes on either button;
  press-and-talk works straight from sleep.

**Audit fixes in this release**
- Day phrases like "sunday afternoon" and "this saturday" now resolve to the right date.
- Sun times work for any date.
- The firmware loop no longer stalls for 3 s on mDNS lookups.
- The Stick recovers after a short Wi-Fi drop.
- A stale backstop card no longer lands on the next question.
- Malformed frames no longer end the session.
- The relay refuses to run with the example device token.
