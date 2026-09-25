# Kai

A pocket AI pal on an **M5StickS3**: hold the button, ask, and Kai answers out loud with a face and
little info cards. The Stick stays thin — it streams audio over home Wi-Fi to a **relay on a home server** (Raspberry Pi or any Linux box),
which runs a **Gemini Live** voice session with Google Search plus Kai's own tools.

```
M5StickS3 ──── WebSocket (LAN) ────▶ home server: kai-relay  ──▶ Gemini Live (gemini-3.8-live)
 mic 16 kHz PCM  ─────────────────▶   ├─ google_search grounding
 speaker 24 kHz  ◀─────────────────   ├─ notes      → SQLite + Markdown per trip
 face + cards    ◀── JSON ──────────   ├─ tides      → NOAA CO-OPS (nearest station)
 buttons                               ├─ weather    → Open-Meteo forecast + marine
                                       ├─ sun/light  → Open-Meteo + astral (golden/blue hour)
                                       └─ parking    → Google Places API (New)
```

## What you can ask

| Ask | Tool | Card on screen |
|---|---|---|
| "Who won the Giants game?" / "Is Sam's Chowder House open?" | Google Search | — |
| "Start a trip called Pigeon Point October" / "Note: f/11, 1/4 s, 10-stop ND at the lighthouse" | `start_trip`, `save_note` | Noted #3 |
| "When's high tide at Pillar Point tomorrow?" | `get_tides` | the day's highs/lows (or the next four) |
| "What's the weather in San Mateo?" / "How's the swell at Half Moon Bay this afternoon?" | `get_weather` | temp + sky, hi/lo, rain, wind, waves, sun |
| "When's golden hour at Pescadero Saturday?" | `get_sun_times` | golden/blue hour, sunset clouds |
| "Find parking near the Ferry Building" | `find_parking` | 5 closest, by distance |

"Here" / "near me" means `KAI_HOME_*` until the phone companion supplies GPS.

## Controls

| Button | Action |
|---|---|
| **Front** (A) hold | talk; release to send. Pressing while Kai talks interrupts it. Taps < 0.3 s are ignored. |
| **Side** (B) click | scroll the reply a page (wraps to the top); from the idle face, reopens the last reply |
| **Side** (B) hold | hush Kai mid-answer; otherwise show the status card (Wi-Fi, relay, battery) |

Every answer is spoken **and** shown: the reply screen has a mini face in the header, the tool's card
(tides, conditions, ...) and then a live transcript of what Kai says. It follows along while Kai talks,
jumps back to the top when it's done, and returns to the face after a minute.

Power: after a minute idle Kai falls asleep and the backlight dims. On battery, after three minutes the
Stick deep-sleeps (Wi-Fi off); either button wakes it in a few seconds. Holding the front button to wake
starts recording immediately, so you can just press and talk: speech is buffered until the relay
reconnects. On USB it never deep-sleeps, it only turns the screen off after four minutes, so it answers
instantly on a desk. Deep sleep drops Gemini's conversation context.

The display is an LCD, so the backlight, not pixel colour, is what costs battery; the dark theme is for
looks. Build with `-DKAI_LIGHT_THEME` for the cream variant.

## Setup

### 1. Relay on a home server

Any always-on Linux box on your LAN works: a Raspberry Pi, a mini PC, a NAS. No sudo needed.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # once
git clone https://github.com/zbruceli/kai ~/kai && cd ~/kai/relay
cp .env.example .env && chmod 600 .env && nano .env   # GEMINI_API_KEY, KAI_DEVICE_TOKEN, KAI_HOME_*
uv sync --frozen
uv run kai-relay                                      # try it; prints ws://<server-ip>:8765/ws
```

Run it at boot as a user service (edit `TZ=` in the file first if you're not on Pacific time; many servers
run in UTC, and "today"/"tomorrow" follow it):

```bash
mkdir -p ~/.config/systemd/user && cp deploy/kai-relay.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload && systemctl --user enable --now kai-relay
journalctl --user -u kai-relay -f                     # live transcript and tool calls
```

To update later: `cd ~/kai && git pull && cd relay && uv sync --frozen && systemctl --user restart kai-relay`.

Give the server a DHCP reservation in your router so its IP never changes; the Stick connects to it.

Keys: Gemini from [AI Studio](https://aistudio.google.com/apikey). Parking needs a Maps key with
**Places API (New)** enabled; without it everything else still works.

### 2. Try it without the Stick

From the Mac, with its mic and speakers:

```bash
cd relay && uv sync --extra sim
uv run kai-sim --host <server-ip> --token <KAI_DEVICE_TOKEN>
```

### 3. Flash the Stick

```bash
cd firmware
cp src/secrets.example.h src/secrets.h   # Wi-Fi, RELAY_HOST, KAI_DEVICE_TOKEN
pio run -t upload && pio device monitor
```

If upload can't find the port: hold the side reset button ~2 s until the green LED blinks (download mode).
`RELAY_HOST` can be the Pi's IP or its `.local` name (resolved over mDNS).

### Tests

```bash
cd relay
uv run pytest               # offline unit tests
uv run pytest -m network -s # hits NOAA / Open-Meteo for real, prints the cards
```

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the pieces fit, the wire protocol, adding a tool
- [docs/POWER.md](docs/POWER.md): power budget, battery life, and how to measure it
- [CHANGELOG.md](CHANGELOG.md)

Releases are source only. The firmware compiles your Wi-Fi password and device token in from
`secrets.h`, so build it yourself; never share a built `firmware.bin`.

## Layout

```
firmware/            PlatformIO, Arduino-ESP32 3.x, M5Unified
  src/main.cpp       modes, buttons, Wi-Fi + WebSocket
  src/audio.*        half-duplex codec: mic streaming, PSRAM speaker ring buffer
  src/face.*         face expressions + info cards (240x135)
relay/
  kai_relay/session.py   device <-> Gemini Live bridge (wire protocol documented at the top)
  kai_relay/persona.py   Kai's system prompt
  kai_relay/backstop.py  runs the right tool itself when Gemini answers a tide/weather/light/parking
                         question without one, so a card always appears
  kai_relay/tools/       one file per capability; add a tool with the @tool decorator
  deploy/                user-level systemd unit for the home server
```

## Roadmap

- [x] M0–M3: push-to-talk voice, search, notes, tides, conditions, light, parking, cards
- [x] Deep sleep between uses, press-and-talk from sleep
- [ ] M4: IMU gestures (shake = cancel, lift = wake), OTA updates, battery measurements
- [ ] Session resumption so context survives Gemini's ~10 min connection limit
- [ ] v2: phone companion as transport + GPS ("near me" for real, location-tagged notes), relay on Cloud Run
      with per-device tokens
- [ ] Optional M5 Unit CAM on the Grove port for "what am I looking at?"

## License

MIT, see [LICENSE](LICENSE).
