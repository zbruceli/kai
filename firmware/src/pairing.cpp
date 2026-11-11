#include "pairing.h"

#include <ArduinoJson.h>
#include <WebSocketsClient.h>
#include <esp_random.h>

extern "C" {
#include <monocypher.h>
}

#include "pairing_crypto.h"
#include "settings.h"

namespace pairing {
namespace {

using namespace paircrypto;

constexpr uint32_t EXCHANGE_TIMEOUT_MS = 20000;
constexpr uint32_t CODE_VALID_MS = 290000;  // the relay keeps the request 300 s

enum class State { Idle, Connecting, AwaitCommit, AwaitNonce, ShowCode, Failed };

WebSocketsClient ws;
State state = State::Idle;
uint32_t startedAt = 0, codeShownAt = 0;
String identity, lastError;

uint8_t priv[32], stickPub[32], stickNonce[16];
uint8_t relayPub[32], relayCommit[64];
uint8_t key[KEY_BYTES];
char codeStr[7] = "";

void wipe() {
  crypto_wipe(priv, sizeof(priv));
  crypto_wipe(key, sizeof(key));
}

void fail(const char* why) {
  log_w("pairing failed: %s", why);
  lastError = why;
  state = State::Failed;
  ws.disconnect();
  wipe();
}

void send(JsonDocument& doc) {
  String out;
  serializeJson(doc, out);
  ws.sendTXT(out);
}

void onText(const uint8_t* payload, size_t length) {
  JsonDocument msg;
  if (deserializeJson(msg, payload, length)) return fail("bad message");
  const char* type = msg["type"] | "";
  if (state == State::AwaitCommit && !strcmp(type, "pair_commit")) {
    if (!fromHex(msg["pub"] | "", relayPub, 32) || !fromHex(msg["commit"] | "", relayCommit, 64)) {
      return fail("bad commitment");
    }
    char nonceHex[33];
    toHex(stickNonce, 16, nonceHex);
    JsonDocument out;
    out["type"] = "pair_nonce";
    out["nonce"] = nonceHex;
    send(out);
    state = State::AwaitNonce;
    log_i("pairing: commitment received, nonce sent");
  } else if (state == State::AwaitNonce && !strcmp(type, "pair_nonce")) {
    uint8_t relayNonce[16];
    if (!fromHex(msg["nonce"] | "", relayNonce, 16)) return fail("bad nonce");
    // The relay committed to its nonce before seeing ours: if it changed it, someone is in between.
    if (!commitmentMatches(relayPub, stickPub, relayNonce, relayCommit)) return fail("commitment mismatch");
    if (!derive(priv, relayPub, identity.c_str(), stickPub, relayPub, stickNonce, relayNonce, key, codeStr)) {
      return fail("bad relay key");
    }
    crypto_wipe(priv, sizeof(priv));
    state = State::ShowCode;
    codeShownAt = millis();
    log_i("pairing: code ready");
    ws.disconnect();  // the exchange is over; the code is what matters now
  } else {
    fail("unexpected message");
  }
}

void onEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED: {
      if (state != State::Connecting) return;
      char pubHex[65];
      toHex(stickPub, 32, pubHex);
      JsonDocument hello;
      hello["type"] = "pair_hello";
      hello["v"] = 1;
      hello["device"] = identity;
      hello["pub"] = pubHex;
      send(hello);
      state = State::AwaitCommit;
      log_i("pairing: connected, hello sent");
      break;
    }
    case WStype_TEXT:
      onText(payload, length);
      break;
    case WStype_DISCONNECTED:
      if (state == State::Connecting || state == State::AwaitCommit || state == State::AwaitNonce) {
        fail("relay closed the exchange");  // refused, rate-limited, or too many requests waiting
      }
      break;
    default:
      break;
  }
}

}  // namespace

void start(const String& host, uint16_t port) {
  identity = settings::deviceName();
  esp_fill_random(priv, sizeof(priv));  // hardware RNG (true random while the radio is on)
  esp_fill_random(stickNonce, sizeof(stickNonce));
  publicKey(priv, stickPub);
  codeStr[0] = '\0';
  lastError = "";
  state = State::Connecting;
  startedAt = millis();
  ws.begin(host, port, "/pair");
  ws.onEvent(onEvent);
  ws.setReconnectInterval(60000);  // one attempt; failures are retried from the caller
}

void poll() {
  if (state == State::Connecting || state == State::AwaitCommit || state == State::AwaitNonce) {
    ws.loop();
    if (millis() - startedAt > EXCHANGE_TIMEOUT_MS) fail("timed out");
  } else if (state == State::ShowCode && millis() - codeShownAt > CODE_VALID_MS) {
    cancel();
    lastError = "code expired";
    state = State::Failed;
  }
}

bool active() { return state != State::Idle && state != State::Failed; }
bool showingCode() { return state == State::ShowCode; }
const char* code() { return codeStr; }
bool failed() { return state == State::Failed; }

const char* status() {
  static String s;
  switch (state) {
    case State::Idle: return "idle";
    case State::Connecting: return "connecting";
    case State::AwaitCommit: return "awaiting commitment";
    case State::AwaitNonce: return "awaiting nonce";
    case State::ShowCode: return "showing code";
    case State::Failed: s = String("failed: ") + lastError; return s.c_str();
  }
  return "?";
}
const char* error() { return lastError.c_str(); }

bool confirm() {
  if (state != State::ShowCode) return false;
  char keyHex[65];
  toHex(key, KEY_BYTES, keyHex);
  const bool ok = settings::setKey(identity, keyHex);
  crypto_wipe(keyHex, sizeof(keyHex));
  wipe();
  state = State::Idle;
  return ok;
}

void cancel() {
  ws.disconnect();
  wipe();
  codeStr[0] = '\0';
  state = State::Idle;
}

}  // namespace pairing
