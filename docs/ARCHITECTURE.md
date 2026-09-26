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

One WebSocket at `/ws`, authenticated with `Authorization: Bearer <KAI_DEVICE_TOKEN>`. The canonical
definition is the docstring at the top of `relay/kai_relay/session.py`.

| Direction | Message | Meaning |
|---|---|---|
| → | `hello {device, fw, battery}` | sent on connect |
| → | `ptt_start`, binary Opus (16 kHz, 20 ms packets, ~24 kbit/s), `ptt_end` | one utterance (front button held) |
| ← | binary Opus (24 kHz, 20 ms packets, ~32 kbit/s) | Kai's voice, ≤ 4 KB per message |
| ← | `caption {delta}` | the next words Kai says (ASCII) |
| ← | `card {title, lines[≤5], icon?, mood?}` | tool result for the screen |
| ← | `interrupted`, `turn_complete`, `state {idle\|thinking}` | turn control |

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

## Security

- Keys live only on the relay (`relay/.env`, git-ignored). The Stick holds just the device token
  (`firmware/src/secrets.h`, git-ignored).
- The device token is compared in constant time, and the relay refuses to start with the example
  token.
- Traffic between the Stick and the relay is plain `ws://`, so keep the relay on a trusted LAN.
