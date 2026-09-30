# Kai architecture

Kai is split so the pocket device stays simple: the **Stick** handles buttons, audio, and the screen; the
**relay** holds the keys and all the logic; **Gemini Live** does speech, reasoning, and search.

```
M5StickS3 (firmware/)            Raspberry Pi or Mac (relay/)                 Google
┌──────────────────────┐  ws://  ┌───────────────────────────────┐  wss://   ┌──────────────┐
│ buttons, mic, speaker├────────▶│ server.py   auth, one session │──────────▶│ Gemini Live  │
│ face + reply screen  │◀────────┤ session.py  device ⇄ Gemini   │◀──────────┤ + Search     │
│ deep sleep           │         │ tools/      notes, tides,     │           └──────────────┘
└──────────────────────┘         │             weather, sun, park├──▶ NOAA, Open-Meteo, Places
                                 │ backstop.py card safety net   │
                                 └───────────────────────────────┘
```

## Wire protocol (Stick ⇄ relay)

One WebSocket at `/ws`. With device keys configured (the default setup), the transport is
`wss://…:8443` with TLS 1.2 ECDHE-PSK:
- **Keys:** a per-device 32-byte pre-shared key (`kai-psk`), so both sides authenticate each other in the
  handshake.
- **Forward secrecy:** X25519, so a key leaked later doesn't decrypt recorded traffic.
- **Cost:** about 0.2 s per reconnect on the Stick (measured).
- **Firmware:** the WebSockets library gets a small build-time patch
  (`firmware/scripts/patch_websockets.py`) that adds `beginSslWithPsk`.

Inside, the Stick still sends `Authorization: Bearer <KAI_DEVICE_TOKEN>`. The canonical
definition is the docstring at the top of `relay/kai_relay/session.py`.

| Direction | Message | Meaning |
|---|---|---|
| → | `hello {device, fw, battery}` | sent on connect |
| → | `ptt_start`, binary Opus (16 kHz, 20 ms packets, ~24 kbit/s), `ptt_end` | one utterance (front button held) |
| ← | binary Opus (24 kHz, 20 ms packets, ~32 kbit/s) | Kai's voice, ≤ 4 KB per message |
| ← | `caption {delta}` | the next words Kai says (ASCII) |
| ← | `card {title, lines[≤5], icon?, mood?}` | tool result for the screen |
| ← | `interrupted`, `turn_complete`, `state {idle\|thinking}` | turn control |
| ← | `inbox {count}` | updates waiting from the background brain (badge) |
| ← | `schedule {wake_in_s}`, `nudge` | when to wake on the RTC timer; chime before a proactive update |

Binary messages start with a kind byte: `0x02` is followed by `[u16 LE length][Opus packet]` entries
(`relay/kai_relay/opus.py`, `firmware/src/voice_codec.cpp`). Firmware advertises `"codecs": ["opus"]` in
`hello`; without it the relay exchanges raw PCM16 (16 kHz up, 24 kHz down), as older firmware and
`kai-sim` do.

## One question, end to end

1. **Press:** the Stick switches the codec to the mic and sends `ptt_start`. The relay opens (or reuses)
   a Gemini Live session.
2. **Talk:** mic audio streams through. Presses under 300 ms count as taps and never reach Gemini.
3. **Release:** the relay sends `activity_end`. Gemini may call a tool: the relay runs it, sends the
   card to the Stick, and returns the data to Gemini.
4. **Answer:** Gemini's audio and transcript stream to the Stick, which plays the audio from a PSRAM
   ring buffer and shows the card plus the transcript.
5. **Safety net:** if Gemini answered a tide, weather, light, or parking question without a tool, no
   card has appeared. `backstop.py` infers the call from the transcript and runs it.

Gemini sometimes marks a tool-call turn complete *before* it speaks. The Stick plays audio whenever it
arrives, so the answer isn't lost.

## Firmware

| File | Role |
|---|---|
| `main.cpp` | Modes `Offline → Idle → Listening → Thinking → Reply`, buttons, Wi-Fi/WebSocket, power |
| `audio.cpp` | Half-duplex ES8311 codec: 3-buffer mic rotation, 2 MB speaker ring, loudness boost |
| `voice_codec.cpp` | Opus (libopus 1.6.1, fixed point): mic encode at complexity 1 (~6.4 ms per 20 ms frame at 240 MHz), speech decode (~3.9 ms) |
| `face.cpp` | 20×18 pixel sprite (pastel), icon bar, reply view with word wrap and scrolling |

**Power (on battery):** dim after 15 s, deep sleep after 45 s; the CPU runs at 80 MHz and Wi-Fi naps whenever no audio streams, and deep sleep cuts the LCD/audio rail (details in `POWER.md`). Buttons (GPIO 11/12) wake it
through EXT1. The Wi-Fi channel/BSSID and the relay IP are kept in RTC memory so reconnecting is fast.
Waking with the front button held records straight into PSRAM, and that audio is sent once the relay
connects. On USB the Stick never deep-sleeps.

## Relay tools

Each tool is an async function registered with `@tool(name, description, params)` in
`relay/kai_relay/tools/`. It returns `ToolResult(data, card)`: `data` goes to Gemini, `card` goes to the
screen.

| Tool | Source | Notes |
|---|---|---|
| `get_weather` | Open-Meteo forecast + marine | temperature first; waves on the coast |
| `get_tides` | NOAA CO-OPS, nearest station ≤ 100 km | one day, or the next four tides |
| `get_sun_times` | astral + Open-Meteo clouds | golden hour and blue hour |
| `find_parking` | Google Places (New) | needs `GOOGLE_MAPS_API_KEY` |
| `save_note`, `list_notes`, `start_trip`, `end_trip` | SQLite + Markdown per trip | |

Places named without a qualifier resolve to the match nearest home, so "San Mateo" means California,
not the Philippines. Days are passed as spoken ("tomorrow", "saturday") and resolved on the relay
(`tools/days.py`), because Gemini's date arithmetic is unreliable.

**Adding a tool:** write the function, give it a `card(..., icon=...)`, import the module in
`tools/__init__.py`, and name it in `persona.py`.

## Background brain (optional: Hermes)

Gemini Live is the **fast path**: it answers in under 2 s with the tools above. For slow work it calls
`ask_agent`, and the relay hands the task to **Hermes Agent**, the **slow path**, running on the same
host:

```
Stick ─ws─▶ relay ──▶ Gemini Live           (fast path, unchanged)
              │ ask_agent ──▶ Hermes API :8642 (POST /v1/runs, polled)   ─┐
              │ ◀── kai_notify, get_tides … ◀── Kai MCP :8766 (localhost) ◀┘ Hermes (Docker, locked down)
              └─ inbox (SQLite): spoken now if a Stick is connected, else a badge
```

- **`agent.py`:**
  - starts runs with a voice-shaped prompt: reply JSON with `speak` (≤ 2 sentences), `card` and
    `details_md`;
  - follows each run, resuming after restarts;
  - turns results into inbox items.
- **Delivery:**
  - the session sends `{"type": "inbox", "count": n}` for the badge;
  - it speaks a result by injecting it into Gemini Live, only when the owner isn't talking and Kai
    isn't mid-answer;
  - it marks the item delivered once Kai has said it.
- **`check_inbox`** reads waiting items on request. The persona mentions waiting updates at the start of
  a session.
- **Only one Stick speaks an update:** the most recently used one (last button press). Every connected
  Stick gets the badge count.
- **Memory:**
  - each Gemini Live conversation is transcribed (owner, Kai, tool calls);
  - when it closes, it goes to Hermes as a *memory* run, which produces no inbox item;
  - Hermes updates its memory and calls `kai_profile_brief` with a one-time key that only that memory
    run gets, so no other run (one that read a hostile web page, say) can rewrite the brief;
  - the relay stores the brief (≤ 800 chars) and puts it in the system prompt of every new
    conversation;
  - `recall` asks Hermes's memory and past sessions, with an 8 s budget before falling back to the
    inbox.
  - "Forget" removes a fact from memory and the brief. Hermes's past-session history is kept.
- **Time (`schedule.py`):**
  - **Reminders** are the relay's own: `remind` takes minutes, or HH:MM plus a spoken day, and the relay
    works out the date. A 15 s loop fires them into the inbox.
  - **Briefings and watches** are Hermes cron jobs named `kai-brief-N` / `kai-watch-N`, with Hermes
    parsing the schedule. They must use Kai's tools for tides, weather and light.
  - A watch replies `[SILENT]` until its condition holds, then calls `kai_notify(watch=…)` with a key
    that's only in its own prompt, and the relay
    deletes the job. Hermes jobs can't remove themselves while `cron.allow_agent_scheduling` stays
    `false`, which it does.
  - `next_wake` is the earliest pending reminder, or a job's next run + 6 min; for watches it's pushed
    past quiet hours (22:00–07:00). Sessions send `{"type": "schedule", "wake_in_s": N}`, and the Stick
    sets its RTC timer before deep sleep.
  - On a timer wake, `hello` carries `"wake": "timer"`. The relay fires reminders due within 60 s, then
    speaks every waiting update, each preceded by `{"type": "nudge"}` (the chime). With nothing due, the
    Stick sleeps again after 10 s.
- **Hermes's lock-down** (`relay/deploy/hermes/`):
  - pinned image (tag and digest), with only its data folder mounted;
  - no terminal, file, code, browser or computer-use tools;
  - manual approvals, and no unattended actions;
  - self-written skills need approval;
  - API and MCP both on localhost, behind tokens.

## Security

- Keys live only on the relay (`relay/.env`, git-ignored). The Stick holds just the device token
  (`firmware/src/secrets.h`, git-ignored).
- The device token is compared in constant time, and the relay refuses to start with the example
  token.
- **Link:** traffic between the Stick and the relay is encrypted and mutually authenticated (TLS-PSK).
  Without device keys the relay falls back to plain `ws://` and says so at startup.
- **Text from outside is data, not commands:**
  - Hermes reads the web, so anything it returns can carry prompt injection.
  - Updates are spoken with tools switched off for that turn.
  - The brief is fenced as facts, and only a memory run can set it.
  - The memory prompt learns only from the owner's own lines.
  - `check_inbox` hands Gemini the short summaries only.
- **Limits:**
  - Per hour: 20 tasks, 20 recalls, 12 memory runs, 20 `kai_notify` updates.
  - At most 10 briefings and watches, checked no more than hourly; watches last at most 14 days.
  - At most 50 pending reminders, set up to 31 days ahead.
  - At most 4 device connections, one per device.
  - 64 KiB messages; audio is accepted only while the button is held.
- **The Stick** limits timer wakes (30 s minimum, and at most 12 in a row without a button press), cuts off
  any reply that runs 3 minutes without a press, and caps caption text at 2 KB.
