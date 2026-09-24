// Kai: a pocket AI pal on the M5StickS3.
// Hold button A to talk, release to send. Button B: click dismisses a card or stops Kai talking,
// hold shows device status. All intelligence lives on the relay (see ../relay).

#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <M5Unified.h>
#include <WebSocketsClient.h>
#include <WiFi.h>

#include "audio.h"
#include "face.h"
#include "secrets.h"

static constexpr const char* FW_VERSION = "0.1.0";
static constexpr uint32_t THINKING_TIMEOUT_MS = 30000;
static constexpr uint32_t CARD_TIMEOUT_MS = 45000;
static constexpr uint32_t DIM_AFTER_MS = 60000;
static constexpr uint32_t STATUS_SHOW_MS = 6000;

enum class Mode { Offline, Idle, Listening, Thinking, Speaking, Card };

static WebSocketsClient ws;
static Mode mode = Mode::Offline;
static bool wsStarted = false;
static bool wsConnected = false;
static bool turnComplete = false;
static Card pendingCard;
static bool hasPendingCard = false;
static uint32_t modeSince = 0;
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
    case Mode::Speaking:  face::setExpr(Expr::Speaking); break;
    case Mode::Card:      break;
  }
  if (m == Mode::Card) {
    face::showCard(pendingCard);
    hasPendingCard = false;
  } else {
    face::hideCard();
  }
  // Modem sleep saves a lot of battery but adds latency; only allow it while nothing is streaming.
  WiFi.setSleep(m == Mode::Idle || m == Mode::Card || m == Mode::Offline);
}

static void wake() {
  lastInteraction = millis();
  if (dimmed) {
    M5.Display.setBrightness(120);
    dimmed = false;
  }
}

static void sendJson(const JsonDocument& doc) {
  String out;
  serializeJson(doc, out);
  ws.sendTXT(out);
}

static void sendType(const char* type) {
  JsonDocument doc;
  doc["type"] = type;
  sendJson(doc);
}

static void sendMic(const int16_t* samples, size_t count) {
  ws.sendBIN(reinterpret_cast<const uint8_t*>(samples), count * sizeof(int16_t));
}

static void finishTurn() {
  setMode(hasPendingCard ? Mode::Card : Mode::Idle);
}

// ---- relay messages --------------------------------------------------------

static void onRelayText(const uint8_t* payload, size_t length) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) return;
  const char* type = doc["type"] | "";

  if (!strcmp(type, "state")) {
    const char* state = doc["state"] | "";
    if (!strcmp(state, "idle") && (mode == Mode::Thinking || mode == Mode::Offline)) finishTurn();
  } else if (!strcmp(type, "card")) {
    pendingCard = Card{};
    pendingCard.title = doc["title"] | "";
    for (JsonVariant line : doc["lines"].as<JsonArray>()) {
      if (pendingCard.count == CARD_LINES) break;
      pendingCard.lines[pendingCard.count++] = line.as<const char*>();
    }
    hasPendingCard = true;
    if (mode == Mode::Idle || mode == Mode::Card) setMode(Mode::Card);  // arrived after speech ended
  } else if (!strcmp(type, "caption")) {
    face::setCaption(doc["text"] | "");
  } else if (!strcmp(type, "interrupted")) {
    audio::clearPlayback();
  } else if (!strcmp(type, "turn_complete")) {
    turnComplete = true;
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
      sendJson(hello);
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
      if (mode == Mode::Thinking || mode == Mode::Speaking) {
        audio::enqueue(payload, length);
        if (mode == Mode::Thinking) setMode(Mode::Speaking);
      }
      break;
    default:
      break;
  }
}

// ---- connectivity ----------------------------------------------------------

static void startRelayConnection() {
  String host = RELAY_HOST;
  if (host.endsWith(".local")) {
    // Arduino's DNS doesn't do mDNS; ask explicitly (e.g. raspberrypi.local).
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
  c.lines[c.count++] = String("Battery ") + String(M5.Power.getBatteryLevel()) + "%" + (M5.Power.isCharging() ? " charging" : "");
  c.lines[c.count++] = String("Firmware ") + FW_VERSION;
  pendingCard = c;
  hasPendingCard = true;
  setMode(Mode::Card);
  modeSince = millis() - (CARD_TIMEOUT_MS - STATUS_SHOW_MS);
}

static void handleButtons() {
  if (M5.BtnA.wasPressed() || M5.BtnB.wasPressed()) wake();

  // A: push to talk (also barges in while Kai is talking).
  if (M5.BtnA.wasPressed() && wsConnected && mode != Mode::Listening) {
    audio::startMic();
    hasPendingCard = false;
    turnComplete = false;
    sendType("ptt_start");
    setMode(Mode::Listening);
  }
  if (M5.BtnA.wasReleased() && mode == Mode::Listening) {
    audio::stopMic(sendMic);
    sendType("ptt_end");
    setMode(Mode::Thinking);
  }

  // B: click = dismiss / hush, hold = status.
  if (M5.BtnB.wasHold()) {
    showStatus();
  } else if (M5.BtnB.wasClicked()) {
    if (mode == Mode::Speaking) {
      audio::clearPlayback();
      turnComplete = true;  // ignore the rest of this answer
      finishTurn();
    } else if (mode == Mode::Card) {
      setMode(Mode::Idle);
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
      if (turnComplete) {
        finishTurn();  // answered with a card only, or nothing to say
      } else if (now - modeSince > THINKING_TIMEOUT_MS) {
        pendingCard = Card{};
        pendingCard.title = "No answer";
        pendingCard.lines[pendingCard.count++] = "The relay went quiet.";
        pendingCard.lines[pendingCard.count++] = "Try again?";
        hasPendingCard = true;
        finishTurn();
      }
      break;
    case Mode::Speaking:
      audio::pollSpeaker(turnComplete);
      face::setLevel(audio::level());
      if (turnComplete && audio::playbackIdle()) finishTurn();
      break;
    case Mode::Card:
      if (now - modeSince > CARD_TIMEOUT_MS) setMode(Mode::Idle);
      break;
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
