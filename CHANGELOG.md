# Changelog

## v0.12.0: encrypted link, security hardening, smarter routing

The Stick now talks to the relay over an encrypted, mutually authenticated link. A full security audit
hardened the relay and firmware against prompt injection from the web, runaway costs and a misbehaving
network. Kai also routes between its fast and slow paths more sensibly.

**Upgrade** (the link changes, so the relay and every Stick move together):
1. **Relay:** `git pull && uv sync --frozen`. It now runs on Python 3.13, which uv installs by itself.
2. **Keys:** for each Stick, run `uv run kai-psk <name>`:
   - put its `KAI_DEVICE_PSKS` line in `relay/.env` (comma-separate several devices);
   - put its two `#define` lines in that Stick's `firmware/src/secrets.h`, together with
     `RELAY_TLS_PORT 8443` (see `secrets.example.h`).
3. **During the switch,** set `KAI_ALLOW_PLAIN_WS=1` so Sticks still on 0.11 can connect; restart the
   relay.
4. **Firmware:** flash 0.12.0 to each Stick.
5. **Finish:** remove `KAI_ALLOW_PLAIN_WS` and restart. Only `wss://…:8443` is served.
6. **With Hermes:** re-run `relay/deploy/hermes/configure.sh`, which applies the new `SOUL.md` and the
   MCP allow-list.

**Encrypted link**
- **The protocol:** TLS 1.2 with ECDHE-PSK (X25519) on `wss://…:8443`, and one 32-byte key per device.
- **What it gives:**
  - both sides prove they hold the key, so nobody on the Wi-Fi can read the audio, pose as the relay
    or a Stick, or steal the token;
  - forward secrecy, so a key leaked later doesn't decrypt past traffic;
  - no certificates, so the Stick needs no clock;
  - revoking a Stick means deleting its key line.
- **Cost, measured on the Stick:** ~0.2 s per reconnect at 240 MHz, about 0.5% battery a day.
  Streaming encryption is negligible, because AES and SHA-256 run in hardware.
- **Firmware:**
  - a build-time patch (`firmware/scripts/patch_websockets.py`) adds PSK to the pinned WebSockets
    library;
  - connecting runs at full CPU speed with the radio awake;
  - mic DMA goes up to ~380 ms, so speech captured while waking survives the handshake.
- **`kai-sim`** uses the encrypted link when given `KAI_SIM_PSK`.

**Security hardening** (from an audit of the firmware, relay, deployment and git history):
- **Prompt injection from the web:**
  - only a memory run, which holds a one-time key, can set Kai's profile brief, and the brief is
    fenced as facts in the prompt;
  - updates are spoken with tools switched off;
  - memory learns from the owner's own lines only;
  - a watch can be stopped only by its own job;
  - Hermes no longer gets the owner's notes.
- **Cost limits:**
  - hourly caps on Hermes runs and on `kai_notify`;
  - briefings and watches no more than hourly, at most 10 active, watches for 14 days at most;
  - at most 50 pending reminders, up to a month ahead.
- **Robustness:**
  - two briefings or watches created in one turn no longer share a name (that left an untracked Hermes
    job running);
  - a malformed Hermes reply can't wedge a task;
  - Hermes errors stay out of what Kai says and shows.
- **Device input:**
  - audio accepted only while the button is held;
  - 64 KiB messages and at most 128 Opus packets per message;
  - one connection per device, 4 in total;
  - device names and telemetry are validated (no CSV-formula or log injection);
  - a bad message no longer drops the connection.
- **The relay's files** are owner-only (umask 077).
- **Firmware:**
  - timer wakes have a 30 s floor and stop after 12 in a row with no button press;
  - a reply that runs 3 minutes with no press is cut off;
  - caption text is capped at 2 KB;
  - no chime while listening or hushed;
  - dependencies are pinned to exact versions and a platform release.

**Fixes**
- **Timer wakes now work on a real Stick.** The Stick never told the relay it had woken on a timer: a
  flag was set one line early. So due reminders weren't fired early and waiting updates weren't spoken.
  Phase 3's tests used a simulated Stick, which is why they missed it.
- **A reminder, note or task is confirmed once.** Gemini Live sometimes repeated the confirmation, and
  the relay now drops the repeat.

**Fast/slow routing**
- **Kai decides by what a good answer needs, not by trigger words:**
  - "research" or "tell me later" always hand off, and Kai never promises a report without starting
    the task;
  - a thin or local "what's new" answer ends with "Want me to dig into it?";
  - "set a timer", "alarm" and "wake me in…" reach `remind`.
- **A routing eval** (`relay/evals/routing.py`) replays requests through Gemini Live and scores the
  path it picks. On 104 cases × 3 runs:

  | | Before | After |
  |---|---|---|
  | Overall | 90% | 94% |
  | Needs digging deeper | 56% | 87% |
  | Explicit hand-offs | 94% | 100% |
  | False hand-offs | 3 | 0 |

**Tested on the home server and the real Stick:**
- spoken questions over `wss://`;
- press-and-talk from deep sleep: the start of the sentence comes through intact over TLS;
- a 5-minute timer woke the sleeping Stick on battery and was spoken;
- plain `ws://`, a wrong key, an unknown device and a certificate-only TLS client were all refused;
- an injected "rewrite the brief" was refused by Hermes and, called directly, by the relay;
- 47 relay tests pass.

## v0.11.0: background brain

Kai gets a slow path next to its fast voice: a local, locked-down Hermes Agent that does long tasks,
remembers you across conversations, and keeps time (reminders, briefings, watches that wake the Stick).

**Upgrade order:** update the relay first (`git pull && uv sync --frozen`, then restart it), then flash
the firmware. Everything in this release is optional: without the `HERMES_*` settings the relay behaves
as in 0.9. To turn it on, follow [relay/deploy/hermes/README.md](relay/deploy/hermes/README.md). Firmware
0.11 works with relay 0.9, just without badges, chimes or timer wakes.

**Phase 3: reminders, briefings, watches**
- **Reminders** ("remind me at 5:30 to pack the ND filters", "in 20 minutes"): the relay keeps them and
  does the date maths itself. Kai says them out loud at the time.
- **Briefings** ("every Saturday at 5:45 give me a fishing brief") and **watches** ("tell me if the
  wind at Half Moon Bay drops under 10 mph this weekend"):
  - both are Hermes scheduled jobs that report through `kai_notify`;
  - a watch stays silent until its condition holds, tells you once, and then the relay removes it;
  - `list_scheduled` and `cancel_scheduled` ("cancel the wind watch") manage them.
- **The Stick wakes itself.** The relay sends the next due time, and the Stick sets its RTC timer
  before deep sleep. On a timer wake it plays a **two-note chime** and Kai speaks what's due; with
  nothing due it goes back to sleep after 10 s.
  - Watches never wake the Stick between 22:00 and 07:00; their news waits for the morning.
- **Tested end to end:**
  - a spoken reminder was delivered live;
  - a timer-wake reconnect 40 s early fired it and spoke it once (after a fix);
  - a briefing and a watch were set, listed and cancelled by voice;
  - a watch fired, was spoken and removed itself via the relay.
- An action is confirmed once: Gemini Live sometimes said "Got it, I'll remind you…" twice (around the
  tool call, then again on its result), so the relay drops the unprompted repeat.
- Firmware 0.11.0.

**Phase 2: memory**

- **Kai remembers across conversations and deep sleeps.** When a conversation ends, its transcript goes
  to Hermes, which updates its memory (it skips small talk, secrets and one-off lookups, and honours
  "forget …"). It then sends back a short **profile brief** (`kai_profile_brief`).
- **Kai's voice starts every conversation with that brief,** so facts about you are answered instantly
  with no tool call. `recall` asks Hermes about anything else from the past: answered live within 8 s,
  otherwise it arrives as an update.
- **Notes stay notes:** "note / jot down" saves a trip note, while "remember that I…" needs no tool.
- **An update is spoken on one Stick only,** the most recently used one; every connected Stick updates
  its badge.
- **Tested end to end on the home server:**
  - remember → the brief was updated;
  - a fresh session answered from the brief without a tool;
  - a slow recall fell back to the inbox and was spoken;
  - "forget" removed the fact from Hermes's memory and from the brief.

**Phase 1: background tasks**

- **Hybrid agent:** Kai's voice (Gemini Live) stays the fast path and hands slow work to a local
  **Hermes Agent** with `ask_agent` ("On it"). Results come back as a spoken answer plus card if the Stick
  is awake, or wait in an inbox (a badge on the face). `check_inbox` reads them.
- **Kai MCP server** in the relay (localhost, token), so Hermes can call `kai_notify` and reuse Kai's
  tide, weather, light and notes tools.
- **Hermes deployment** in `relay/deploy/hermes/`: pinned Docker image (tag and digest) that sees only
  its data folder, with no shell, file, code, browser or computer-use tools, manual approvals and no
  unattended actions; `configure.sh` applies and checks it.
- **Tested end to end** on the home server: a spoken research request was handed off, Hermes answered in
  ~45 s with web research, and Kai spoke the result on its own. Hermes also called Kai's `get_tides` and
  `kai_notify`. Red-team requests for files and SSH keys had no tool to use.
- Firmware 0.10.0: inbox badge.
- All optional: without the Hermes settings, the relay behaves as before.

## v0.9.0: Opus voice

**Upgrade order:** update the relay first (`git pull && uv sync --frozen`, which adds PyAV, and restart it),
then flash the firmware. Relay 0.9 still serves older firmware over raw PCM, but firmware 0.9 needs relay
0.9.

- Opus voice between the Stick and the relay: 16 kHz mic at ~24 kbit/s (was 256) and 24 kHz speech at
  ~32 kbit/s (was 384), about 11–12x less Wi-Fi airtime. On the Stick at 240 MHz, encoding takes ~6.4 ms
  and decoding ~3.9 ms per 20 ms frame (measured). Older firmware and `kai-sim` keep raw PCM.
- Measurement builds log Opus encode/decode time per frame (`enc_us`, `dec_us`).
- Kai's first sentences no longer stutter. Gemini sends an answer 3–4x faster than real time, and
  decoding each Opus message on arrival starved the speaker. Packets are now queued compressed and
  decoded just in time: 0 speaker underruns over four real answers (measured).
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
