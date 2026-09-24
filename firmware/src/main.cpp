// Kai: a pocket AI pal on the M5StickS3.
// Front button (A): hold to talk, release to send; pressing while Kai talks interrupts it.
// Side button (B): click scrolls the reply (or reopens the last one); hold hushes Kai, or shows status.
// All intelligence lives on the relay (see ../relay).

#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <M5Unified.h>
#include <WebSocketsClient.h>
#include <WiFi.h>

#include "audio.h"
#include "face.h"
#include "secrets.h"

static constexpr const char* FW_VERSION = "0.2.0";
static constexpr uint32_t THINKING_TIMEOUT_MS = 30000;
static constexpr uint32_t EMPTY_TURN_GRACE_MS = 2000;  // turn ended with nothing to show: wait for stragglers
static constexpr uint32_t REPLY_TIMEOUT_MS = 60000;    // reply screen returns to the face after this idle time
static constexpr uint32_t DIM_AFTER_MS = 60000;

enum class Mode { Offline, Idle, Listening, Thinking, Reply };

static WebSocketsClient ws;
static Mode mode = Mode::Offline;
static bool wsStarted = false;
static bool wsConnected = false;
static bool turnComplete = false;
static uint32_t turnCompleteAt = 0;
static bool hushed = false;  // user silenced this answer: drop the rest of it
static bool wasSpeaking = false;
static uint32_t modeSince = 0;
static uint32_t lastReplyActivity = 0;
static uint32_t lastInteraction = 0;
static uint32_t lastBatteryRead = 0;
static bool dimmed = false;

// ---------------------------------------------------------------------------

static void setMode(Mode m) {
  mode = m;
  modeSince = millis();
  switch (m) {
    case Mode::Offline:   face::setExpr(Expr::Offline); break;
    case Mode::Idle:      face::setExpr(Expr::Idle); break;
    case Mode::Listening: face::setExpr(Expr::Listening); break;
    case Mode::Thinking:  face::setExpr(Expr::Thinking); break;
    case Mode::Reply:     lastReplyActivity = millis(); break;
  }
  face::showReply(m == Mode::Reply);
  // Modem sleep saves battery but adds latency; only allow it while nothing is streaming.
  WiFi.setSleep(m == Mode::Idle || m == Mode::Offline);
}

static void wake() {
  lastInteraction = millis();
  if (dimmed) {
    M5.Display.setBrightness(120);
    dimmed = false;
  }
}

static void sendType(const char* type) {
  JsonDocument doc;
  doc["type"] = type;
  String out;
  serializeJson(doc, out);
  ws.sendTXT(out);
}

static void sendMic(const int16_t* samples, size_t count) {
  ws.sendBIN(reinterpret_cast<const uint8_t*>(samples), count * sizeof(int16_t));
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

// ---- relay messages --------------------------------------------------------

static void onRelayText(const uint8_t* payload, size_t length) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) return;
  const char* type = doc["type"] | "";

  if (!strcmp(type, "card")) {
    if (hushed || mode == Mode::Listening) return;
    Card c;
    c.title = doc["title"] | "";
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
      JsonDocument hello;
      hello["type"] = "hello";
      hello["device"] = String("kai-") + WiFi.macAddress().substring(12);
      hello["fw"] = FW_VERSION;
      hello["battery"] = M5.Power.getBatteryLevel();
      String out;
      serializeJson(hello, out);
      ws.sendTXT(out);
      face::setStatusText("");
      setMode(Mode::Idle);
      break;
    }
    case WStype_DISCONNECTED:
      if (wsConnected) log_w("relay disconnected");
      wsConnected = false;
      if (mode == Mode::Listening) audio::stopMic([](const int16_t*, size_t) {});
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

static void startRelayConnection() {
  String host = RELAY_HOST;
  if (host.endsWith(".local")) {
    // Arduino's DNS doesn't do mDNS; ask explicitly.
    IPAddress ip = MDNS.queryHost(host.substring(0, host.length() - 6), 3000);
    if (ip == IPAddress()) {
      face::setStatusText(String("can't find ") + host);
      return;  // retried from loop()
    }
    host = ip.toString();
  }
  ws.begin(host, RELAY_PORT, "/ws");
  ws.setExtraHeaders("Authorization: Bearer " KAI_DEVICE_TOKEN);
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 3000, 2);
  wsStarted = true;
  face::setStatusText("finding relay...");
}

static void maintainConnection() {
  static uint32_t lastTry = 0;
  if (WiFi.status() != WL_CONNECTED) {
    if (mode != Mode::Offline) setMode(Mode::Offline);
    face::setStatusText("connecting wifi...");
    return;
  }
  if (!wsStarted && millis() - lastTry > 5000) {
    lastTry = millis();
    static bool mdnsStarted = false;
    if (!mdnsStarted) mdnsStarted = MDNS.begin("kai-stick");
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
                       (M5.Power.isCharging() ? " charging" : "");
  c.lines[c.count++] = String("Firmware ") + FW_VERSION;
  face::clearReply();
  showCard(c);
}

static void handleButtons() {
  if (M5.BtnA.wasPressed() || M5.BtnB.wasPressed()) wake();

  // Front: push to talk (also barges in while Kai is talking).
  if (M5.BtnA.wasPressed() && wsConnected && mode != Mode::Listening) {
    audio::startMic();
    face::clearReply();
    hushed = false;
    turnComplete = false;
    wasSpeaking = false;
    sendType("ptt_start");
    setMode(Mode::Listening);
  }
  if (M5.BtnA.wasReleased() && mode == Mode::Listening) {
    audio::stopMic(sendMic);
    sendType("ptt_end");
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

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  face::begin();
  if (!audio::begin()) log_e("PSRAM allocation failed: check board_build.arduino.memory_type");
  face::setBattery(M5.Power.getBatteryLevel());
  face::setStatusText("connecting wifi...");
  setMode(Mode::Offline);
  lastInteraction = millis();
}

void loop() {
  M5.update();
  maintainConnection();
  if (wsStarted) ws.loop();
  handleButtons();

  uint32_t now = millis();
  switch (mode) {
    case Mode::Listening:
      audio::pollMic(sendMic);
      face::setLevel(audio::level());
      break;

    case Mode::Thinking:
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
  }
  if (!dimmed && mode == Mode::Idle && now - lastInteraction > DIM_AFTER_MS) {
    M5.Display.setBrightness(20);
    dimmed = true;
  }

  face::render();
}
