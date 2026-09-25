// Kai: a pocket AI pal on the M5StickS3.
// Front button (A): hold to talk, release to send; pressing while Kai talks interrupts it.
// Side button (B): click scrolls the reply (or reopens the last one); hold hushes Kai, or shows status.
// All intelligence lives on the relay (see ../relay).

#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <M5Unified.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <driver/rtc_io.h>
#include <esp_heap_caps.h>
#include <esp_sleep.h>
#include <sys/time.h>

#include "audio.h"
#include "face.h"
#include "secrets.h"

static constexpr const char* FW_VERSION = "0.7.0";
static constexpr uint32_t THINKING_TIMEOUT_MS = 30000;
static constexpr uint32_t EMPTY_TURN_GRACE_MS = 2000;  // turn ended with nothing to show: wait for stragglers
static constexpr uint32_t REPLY_TIMEOUT_MS = 60000;    // reply screen returns to the face after this idle time

// Power: on this LCD the backlight costs battery (pixel colour doesn't), and the Wi-Fi radio costs even
// more. On battery Kai dims, then deep-sleeps; on USB it only turns the screen off so it stays instant.
static constexpr uint8_t BRIGHTNESS_AWAKE = 90;
static constexpr uint8_t BRIGHTNESS_DIM = 15;
static constexpr uint32_t DIM_AFTER_MS = 60000;
static constexpr uint32_t DEEP_SLEEP_AFTER_MS = 3 * 60000;  // on battery
static constexpr uint32_t SCREEN_OFF_AFTER_MS = 4 * 60000;  // on USB

// Power telemetry: battery voltage and state are reported to the relay (relay/data/power.csv) so
// consumption per state can be measured from the voltage slope; see docs/POWER.md.
static constexpr uint32_t POWER_REPORT_MS = 30000;

// Buttons (active low), both RTC-capable so they can wake the chip from deep sleep.
static constexpr gpio_num_t PIN_BTN_A = GPIO_NUM_11;
static constexpr gpio_num_t PIN_BTN_B = GPIO_NUM_12;

// Waking with the front button held starts recording at once; speech is kept here until the relay is
// reachable, then sent as one utterance.
static constexpr uint32_t EARLY_TALK_MAX_MS = 15000;
static constexpr size_t EARLY_TALK_BYTES = audio::MIC_RATE * 2 * EARLY_TALK_MAX_MS / 1000;

// Survive deep sleep: skip the Wi-Fi scan and the mDNS lookup on wake.
RTC_DATA_ATTR static uint8_t cachedBssid[6];
RTC_DATA_ATTR static int32_t cachedChannel = 0;
RTC_DATA_ATTR static uint32_t cachedRelayIp = 0;
RTC_DATA_ATTR static int64_t sleptAtUs = 0;  // RTC wall clock keeps running through deep sleep
RTC_DATA_ATTR static int16_t sleptMv = 0;

enum class Mode { Offline, Idle, Listening, Thinking, Reply };

static WebSocketsClient ws;
static Mode mode = Mode::Offline;
static bool wsStarted = false;
static bool wsConnected = false;
static bool everConnected = false;
static bool wokeFromSleep = false;
static uint32_t wsBeganAt = 0;
static bool usedRelayCache = false;
static bool connectedSinceBegin = false;
static uint32_t wifiBeganAt = 0;
static bool usedWifiCache = false;
static bool wifiCached = false;
static bool turnComplete = false;
static uint32_t turnCompleteAt = 0;
static bool hushed = false;  // user silenced this answer: drop the rest of it
static bool wasSpeaking = false;
static uint32_t modeSince = 0;
static uint32_t lastReplyActivity = 0;
static uint32_t lastInteraction = 0;
static uint32_t lastBatteryRead = 0;
static bool dimmed = false;
static bool screenOff = false;
static uint8_t brightness = 0;
static int16_t wakeMv = 0;  // battery voltage at boot, before Wi-Fi loads it
static uint32_t lastPowerReport = 0;
static uint32_t loopCount = 0;
static uint32_t renderBusyUs = 0;

static bool earlyTalk = false;       // capturing speech before the relay is connected
static bool earlyTalkEnded = false;  // ...and the button was already released
static uint32_t earlyTalkSince = 0;
static uint8_t* earlyBuf = nullptr;
static size_t earlyLen = 0;

// ---------------------------------------------------------------------------

static void setMode(Mode m) {
  mode = m;
  modeSince = millis();
  lastInteraction = modeSince;  // e.g. a reply timing out shouldn't dim or deep-sleep at once
  switch (m) {
    // Freshly woken and still reconnecting: Kai is waking up, not broken.
    case Mode::Offline:   face::setExpr(wokeFromSleep && !everConnected ? Expr::Sleeping : Expr::Offline); break;
    case Mode::Idle:      face::setExpr(Expr::Idle); break;
    case Mode::Listening: face::setExpr(Expr::Listening); break;
    case Mode::Thinking:  face::setExpr(Expr::Thinking); break;
    case Mode::Reply:     lastReplyActivity = millis(); break;
  }
  face::showReply(m == Mode::Reply);
  // Modem sleep saves battery but adds latency; only allow it while nothing is streaming.
  WiFi.setSleep(m == Mode::Idle || m == Mode::Offline);
}

static void backlight(uint8_t level) {
  brightness = level;
  M5.Display.setBrightness(level);
}

static int64_t rtcNowUs() {
  timeval tv;
  gettimeofday(&tv, nullptr);
  return int64_t(tv.tv_sec) * 1000000 + tv.tv_usec;
}

static void wake() {
  lastInteraction = millis();
  if (screenOff) {
    M5.Display.wakeup();
    screenOff = false;
  }
  if (dimmed) {
    backlight(BRIGHTNESS_AWAKE);
    dimmed = false;
    if (mode == Mode::Idle) face::setExpr(Expr::Idle);
  }
}

[[noreturn]] static void goToDeepSleep() {
  log_i("deep sleep");
  if (wsConnected) ws.disconnect();
  sleptMv = M5.Power.getBatteryVoltage();
  sleptAtUs = rtcNowUs();
  audio::clearPlayback();
  M5.Speaker.end();
  M5.Mic.end();
  backlight(0);
  M5.Display.sleep();
  M5.Display.waitDisplay();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);

  // A button still held would wake us straight back up.
  while (digitalRead(PIN_BTN_A) == LOW || digitalRead(PIN_BTN_B) == LOW) delay(10);

  const uint64_t mask = (1ULL << PIN_BTN_A) | (1ULL << PIN_BTN_B);
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 3, 0)
  esp_sleep_enable_ext1_wakeup_io(mask, ESP_EXT1_WAKEUP_ANY_LOW);
#else
  esp_sleep_enable_ext1_wakeup(mask, ESP_EXT1_WAKEUP_ANY_LOW);
#endif
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON);  // keeps the pull-ups alive
  for (gpio_num_t pin : {PIN_BTN_A, PIN_BTN_B}) {
    rtc_gpio_pullup_en(pin);
    rtc_gpio_pulldown_dis(pin);
  }
  esp_deep_sleep_start();
}

static void powerSave(uint32_t now) {
#ifdef KAI_POWER_TEST
  return;  // hold the current state for a measurement run (env:sticks3_powertest)
#endif
  if (mode != Mode::Idle && mode != Mode::Offline) return;
  if (earlyTalk) return;
  const uint32_t idle = now - lastInteraction;
  if (!dimmed && idle > DIM_AFTER_MS) {
    backlight(BRIGHTNESS_DIM);
    if (mode == Mode::Idle) face::setExpr(Expr::Sleeping);
    dimmed = true;
  }
  if (audio::onUsbPower()) {
    if (!screenOff && idle > SCREEN_OFF_AFTER_MS) {
      backlight(0);
      M5.Display.sleep();
      screenOff = true;
    }
  } else if (idle > DEEP_SLEEP_AFTER_MS) {
    goToDeepSleep();
  }
}

static Icon iconFromName(const char* name) {
  if (!strcmp(name, "mic")) return Icon::Mic;
  if (!strcmp(name, "weather")) return Icon::Weather;
  if (!strcmp(name, "fishing")) return Icon::Fishing;
  if (!strcmp(name, "camera")) return Icon::Camera;
  return Icon::None;
}

static void sendType(const char* type) {
  JsonDocument doc;
  doc["type"] = type;
  String out;
  serializeJson(doc, out);
  ws.sendTXT(out);
}

static void sendMic(const int16_t* samples, size_t count) {
  const size_t bytes = count * sizeof(int16_t);
  if (earlyTalk) {
    if (earlyBuf && earlyLen + bytes <= EARLY_TALK_BYTES) {
      memcpy(earlyBuf + earlyLen, samples, bytes);
      earlyLen += bytes;
    }
    return;
  }
  ws.sendBIN(reinterpret_cast<const uint8_t*>(samples), bytes);
}

static void discardMic(const int16_t*, size_t) {}

static const char* modeName(Mode m) {
  switch (m) {
    case Mode::Offline: return "offline";
    case Mode::Idle: return dimmed ? "idle_dim" : "idle";
    case Mode::Listening: return "listening";
    case Mode::Thinking: return "thinking";
    case Mode::Reply: return audio::playbackIdle() ? "reply" : "speaking";
  }
  return "?";
}

static void sendPowerReport(uint32_t now) {
  const uint32_t span = now - lastPowerReport;
  JsonDocument doc;
  doc["type"] = "power";
  doc["mv"] = M5.Power.getBatteryVoltage();
  doc["usb"] = audio::onUsbPower();
  doc["mode"] = modeName(mode);
  doc["bright"] = screenOff ? 0 : brightness;
  doc["wifi_ps"] = WiFi.getSleep();
  doc["rssi"] = WiFi.RSSI();
  doc["up_s"] = now / 1000;
  doc["loop_hz"] = span ? loopCount * 1000 / span : 0;
  doc["render_pct"] = span ? renderBusyUs / 10 / span : 0;  // % of time spent drawing
  String out;
  serializeJson(doc, out);
  ws.sendTXT(out);
  lastPowerReport = now;
  loopCount = 0;
  renderBusyUs = 0;
}

// Anything Kai sends back (speech, words, a card) lands on the reply screen, whatever we were showing.
static void replyActivity() {
  lastReplyActivity = millis();
  if (mode != Mode::Reply) setMode(Mode::Reply);
}

static void showCard(const Card& c) {
  face::setReplyCard(c);
  replyActivity();
}

static void beginTurn() {
  face::clearReply();
  hushed = false;
  turnComplete = false;
  wasSpeaking = false;
}

// ---- early talk (woken by holding the front button) ----------------------

static void startEarlyTalk() {
  if (!earlyBuf) earlyBuf = static_cast<uint8_t*>(heap_caps_malloc(EARLY_TALK_BYTES, MALLOC_CAP_SPIRAM));
  earlyLen = 0;
  earlyTalk = true;
  earlyTalkEnded = false;
  earlyTalkSince = millis();
  beginTurn();
  audio::startMic();
  face::setStatusText("");
  setMode(Mode::Listening);
}

// Relay just connected: replay what was said while we were waking up, then carry on live.
static void flushEarlyTalk() {
  earlyTalk = false;
  sendType("ptt_start");
  for (size_t i = 0; i < earlyLen; i += 4096) ws.sendBIN(earlyBuf + i, min<size_t>(4096, earlyLen - i));
  earlyLen = 0;
  if (earlyTalkEnded) {
    sendType("ptt_end");
    setMode(Mode::Thinking);  // restart the timeout from now
  }
}

static void abandonEarlyTalk() {
  if (mode == Mode::Listening) audio::stopMic(discardMic);
  earlyTalk = false;
  earlyLen = 0;
  setMode(Mode::Offline);
}

// ---- relay messages --------------------------------------------------------

static void onRelayText(const uint8_t* payload, size_t length) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) return;
  const char* type = doc["type"] | "";

  if (!strcmp(type, "card")) {
    if (hushed || mode == Mode::Listening) return;
    Card c;
    c.title = doc["title"] | "";
    c.icon = iconFromName(doc["icon"] | "");
    c.happy = !strcmp(doc["mood"] | "", "happy");
    for (JsonVariant line : doc["lines"].as<JsonArray>()) {
      if (c.count == CARD_LINES) break;
      c.lines[c.count++] = line.as<const char*>();
    }
    showCard(c);
  } else if (!strcmp(type, "caption")) {
    if (hushed || mode == Mode::Listening) return;
    face::appendReplyText(doc["delta"] | "");
    replyActivity();
  } else if (!strcmp(type, "interrupted")) {
    audio::clearPlayback();
  } else if (!strcmp(type, "turn_complete")) {
    turnComplete = true;
    turnCompleteAt = millis();
  } else if (!strcmp(type, "state")) {
    // "idle" = the relay dropped the turn (a tap, or an error it already sent a card for).
    if (!strcmp(doc["state"] | "", "idle") && mode == Mode::Thinking) {
      setMode(face::hasReply() ? Mode::Reply : Mode::Idle);
    }
  }
}

static void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED: {
      wsConnected = true;
      everConnected = true;
      connectedSinceBegin = true;
      JsonDocument hello;
      hello["type"] = "hello";
      hello["device"] = String("kai-") + WiFi.macAddress().substring(12);
      hello["fw"] = FW_VERSION;
      hello["battery"] = M5.Power.getBatteryLevel();
      if (wokeFromSleep && sleptAtUs && !everConnected) {  // one deep-sleep measurement per wake
        JsonObject sleep = hello["sleep"].to<JsonObject>();
        sleep["slept_s"] = (rtcNowUs() - sleptAtUs) / 1000000;
        sleep["mv_before"] = sleptMv;
        sleep["mv_after"] = wakeMv;
      }
      String out;
      serializeJson(hello, out);
      ws.sendTXT(out);
      face::setStatusText("");
      if (earlyTalk) {
        flushEarlyTalk();
      } else {
        setMode(Mode::Idle);
      }
      break;
    }
    case WStype_DISCONNECTED:
      if (!wsConnected) break;  // a failed connection attempt; keep retrying quietly
      log_w("relay disconnected");
      wsConnected = false;
      if (mode == Mode::Listening) audio::stopMic(discardMic);
      audio::clearPlayback();
      face::setStatusText("finding relay...");
      setMode(Mode::Offline);
      break;
    case WStype_TEXT:
      onRelayText(payload, length);
      break;
    case WStype_BIN:
      // Speech is played whenever it arrives, even after a card or a turn_complete: Gemini can finish a
      // tool-call turn first and speak the answer in the next one.
      if (hushed || mode == Mode::Listening || mode == Mode::Offline) break;
      audio::enqueue(payload, length);
      turnComplete = false;
      replyActivity();
      break;
    default:
      break;
  }
}

// ---- connectivity ----------------------------------------------------------

static void startWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  wifiBeganAt = millis();
  if (cachedChannel > 0) {
    WiFi.begin(WIFI_SSID, WIFI_PASS, cachedChannel, cachedBssid);  // skips the channel scan
    usedWifiCache = true;
  } else {
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
}

static void startRelayConnection() {
  String host = RELAY_HOST;
  usedRelayCache = false;
  if (host.endsWith(".local")) {
    if (cachedRelayIp) {
      host = IPAddress(cachedRelayIp).toString();
      usedRelayCache = true;
    } else {
      // Arduino's DNS doesn't do mDNS; ask explicitly.
      static bool mdnsStarted = false;
      if (!mdnsStarted) mdnsStarted = MDNS.begin("kai-stick");
      // Blocks the loop (no redraw, no mic) while it waits, so keep it short and retry instead.
      IPAddress ip = MDNS.queryHost(host.substring(0, host.length() - 6), 1000);
      if (ip == IPAddress()) {
        if (!earlyTalk) face::setStatusText(String("can't find ") + host);
        return;  // retried from loop()
      }
      cachedRelayIp = uint32_t(ip);
      host = ip.toString();
    }
  }
  ws.begin(host, RELAY_PORT, "/ws");
  ws.setExtraHeaders("Authorization: Bearer " KAI_DEVICE_TOKEN);
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(wokeFromSleep ? 1000 : 3000);
  ws.enableHeartbeat(15000, 3000, 2);
  wsStarted = true;
  wsBeganAt = millis();
  connectedSinceBegin = false;
  if (!earlyTalk) face::setStatusText(wokeFromSleep ? "waking up..." : "finding relay...");
}

static void maintainConnection() {
  static uint32_t lastTry = 0;
  const uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    if (usedWifiCache && now - wifiBeganAt > 5000) {  // access point moved: fall back to a full scan
      usedWifiCache = false;
      cachedChannel = 0;
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASS);
    }
    if (!earlyTalk) {
      if (mode == Mode::Listening) audio::stopMic(discardMic);
      if (mode != Mode::Offline) setMode(Mode::Offline);
      face::setStatusText(wokeFromSleep && !everConnected ? "waking up..." : "connecting wifi...");
    }
    return;
  }
  if (wsConnected && mode == Mode::Offline) {  // Wi-Fi blipped but the relay socket survived
    face::setStatusText("");
    setMode(Mode::Idle);
  }
  if (!wifiCached) {
    memcpy(cachedBssid, WiFi.BSSID(), 6);
    cachedChannel = WiFi.channel();
    wifiCached = true;
  }

  // The relay may have a new address since we cached it: drop the cache and look it up again.
  if (wsStarted && !connectedSinceBegin && usedRelayCache && now - wsBeganAt > 6000) {
    ws.disconnect();
    wsStarted = false;
    cachedRelayIp = 0;
    lastTry = 0;
  }
  if (!wsStarted && (lastTry == 0 || now - lastTry > 5000)) {
    lastTry = now;
    startRelayConnection();
  }
}

// ---- buttons ---------------------------------------------------------------

static void showStatus() {
  Card c;
  c.title = "Kai status";
  c.lines[c.count++] = String("WiFi ") + WiFi.localIP().toString() + " " + String(WiFi.RSSI()) + "dB";
  c.lines[c.count++] = String("Relay ") + (wsConnected ? "connected" : "offline");
  c.lines[c.count++] = String("Battery ") + String(M5.Power.getBatteryLevel()) + "%" +
                       (audio::onUsbPower() ? " on USB" : "");
  c.lines[c.count++] = String("Firmware ") + FW_VERSION;
  face::clearReply();
  showCard(c);
}

static void handleButtons() {
  // With the screen off, the side button only wakes Kai (so it doesn't also scroll or open status).
  if (screenOff && M5.BtnB.wasPressed()) {
    wake();
    return;
  }
  if (M5.BtnA.wasPressed() || M5.BtnB.wasPressed()) wake();

  // Front: push to talk (also barges in while Kai is talking).
  if (M5.BtnA.wasPressed() && wsConnected && mode != Mode::Listening) {
    beginTurn();
    audio::startMic();
    sendType("ptt_start");
    setMode(Mode::Listening);
  }
  if (M5.BtnA.wasReleased() && mode == Mode::Listening) {
    audio::stopMic(sendMic);
    if (earlyTalk) {
      earlyTalkEnded = true;  // sent as soon as the relay connects
    } else {
      sendType("ptt_end");
    }
    setMode(Mode::Thinking);
  }

  // Side: click = scroll, hold = hush while talking, otherwise status.
  if (M5.BtnB.wasHold()) {
    if (mode == Mode::Reply && !audio::playbackIdle()) {
      audio::clearPlayback();
      hushed = true;
    } else if (mode != Mode::Listening) {
      showStatus();
    }
  } else if (M5.BtnB.wasClicked()) {
    if (mode == Mode::Reply) {
      face::scrollReply();
      lastReplyActivity = millis();
    } else if (mode == Mode::Idle && face::hasReply()) {
      setMode(Mode::Reply);  // bring back the last answer
    }
  }
}

// ---------------------------------------------------------------------------

void setup() {
  auto cfg = M5.config();
  cfg.fallback_board = m5::board_t::board_M5StickS3;
  M5.begin(cfg);
  Serial.begin(115200);

  wakeMv = M5.Power.getBatteryVoltage();
  bool wokeToTalk = false;
  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT1) {
    wokeFromSleep = true;
    wokeToTalk = esp_sleep_get_ext1_wakeup_status() & (1ULL << PIN_BTN_A);
  }

  startWifi();
  face::begin();
  backlight(BRIGHTNESS_AWAKE);
  if (!audio::begin()) log_e("PSRAM allocation failed: check board_build.arduino.memory_type");
  face::setBattery(M5.Power.getBatteryLevel());
  lastInteraction = millis();

  M5.update();
  if (wokeToTalk && M5.BtnA.isPressed()) {
    startEarlyTalk();
  } else {
    face::setStatusText(wokeFromSleep ? "waking up..." : "connecting wifi...");
    setMode(Mode::Offline);
  }
}

void loop() {
  M5.update();
  maintainConnection();
  if (wsStarted) ws.loop();
  handleButtons();

  uint32_t now = millis();
  if (earlyTalk && now - earlyTalkSince > EARLY_TALK_MAX_MS) abandonEarlyTalk();

  switch (mode) {
    case Mode::Listening:
      audio::pollMic(sendMic);
      face::setLevel(audio::level());
      break;

    case Mode::Thinking:
      if (earlyTalk) break;  // still waiting for the relay; abandonEarlyTalk() handles the timeout
      if (turnComplete && now - turnCompleteAt > EMPTY_TURN_GRACE_MS) {
        setMode(Mode::Idle);  // nothing came back
      } else if (now - modeSince > THINKING_TIMEOUT_MS) {
        Card c;
        c.title = "No answer";
        c.lines[c.count++] = "The relay went quiet.";
        c.lines[c.count++] = "Try again?";
        showCard(c);
      }
      break;

    case Mode::Reply: {
      audio::pollSpeaker(turnComplete);
      bool speaking = !audio::playbackIdle();
      face::setSpeaking(speaking);
      face::setLevel(audio::level());
      if (speaking) {
        lastReplyActivity = now;
        wasSpeaking = true;
      } else if (wasSpeaking && turnComplete) {
        face::replyFinished();  // only at the real end, not a mid-answer buffer gap
        wasSpeaking = false;
      }
      if (!speaking && now - lastReplyActivity > REPLY_TIMEOUT_MS) setMode(Mode::Idle);
      break;
    }

    default:
      face::setLevel(0);
      break;
  }

  if (now - lastBatteryRead > 30000) {
    lastBatteryRead = now;
    face::setBattery(M5.Power.getBatteryLevel());
    if (mode != Mode::Listening) audio::updateVolume();  // follows USB plug/unplug
  }
  powerSave(now);
  loopCount++;
  if (!screenOff) {
    const uint32_t t0 = micros();
    face::render();
    renderBusyUs += micros() - t0;
  }
  if (wsConnected && now - lastPowerReport >= POWER_REPORT_MS) sendPowerReport(now);
}
