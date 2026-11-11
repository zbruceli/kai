#include "provision.h"

#include <ArduinoJson.h>
#include <WiFi.h>
#include <improv.h>

#include <vector>

#include "pairing.h"
#include "provision_util.h"
#include "settings.h"

namespace provision {
namespace {

constexpr uint32_t JOIN_TIMEOUT_MS = 20000;
constexpr size_t MAX_NETWORKS = 20;
constexpr const char* LINE_PREFIX = "kai-config ";

const char* fwVersion = "";
Hooks hooks{};

uint8_t frameBuf[256];
size_t framePos = 0;
bool haveCommand = false;
improv::ImprovCommand command;

String line;

bool joining = false;
uint32_t joinStartedAt = 0;
String joinSsid, joinPass;

// ---- Improv: replies ----

void sendFrame(uint8_t type, const uint8_t* data, size_t len) {
  uint8_t out[300];
  const size_t n = provutil::improvFrame(improv::IMPROV_SERIAL_VERSION, type, data, len, out);
  Serial.write(out, n);
  Serial.write('\n');  // keeps the stream readable for logs; Improv readers resync on the header
  Serial.flush();
}

void sendState(improv::State state) {
  const uint8_t s = state;
  sendFrame(improv::TYPE_CURRENT_STATE, &s, 1);
}

void sendError(improv::Error error) {
  const uint8_t e = error;
  sendFrame(improv::TYPE_ERROR_STATE, &e, 1);
}

// RPC result, per the spec: command, data length, then each string prefixed by its length.
void sendResult(improv::Command cmd, const std::vector<String>& strings) {
  uint8_t data[256];
  size_t n = 2;
  for (const String& s : strings) {
    const size_t len = min<size_t>(s.length(), 255);
    if (n + 1 + len > sizeof(data)) break;
    data[n++] = uint8_t(len);
    memcpy(data + n, s.c_str(), len);
    n += len;
  }
  data[0] = cmd;
  data[1] = uint8_t(n - 2);
  sendFrame(improv::TYPE_RPC_RESPONSE, data, n);
}

bool provisioned() { return settings::hasWifi() && WiFi.status() == WL_CONNECTED; }

// ---- Improv: commands ----

void handleImprov(const improv::ImprovCommand& cmd) {
  switch (cmd.command) {
    case improv::GET_CURRENT_STATE:
      if (provisioned()) {
        sendState(improv::STATE_PROVISIONED);
        sendResult(improv::GET_CURRENT_STATE, {});
      } else {
        sendState(joining ? improv::STATE_PROVISIONING : improv::STATE_AUTHORIZED);
      }
      break;
    case improv::GET_DEVICE_INFO:
      sendResult(improv::GET_DEVICE_INFO, {"Kai", fwVersion, "ESP32-S3", settings::deviceName()});
      break;
    case improv::GET_WIFI_NETWORKS: {
      const int found = WiFi.scanNetworks();  // ~2 s; fine during setup
      std::vector<String> seen;
      for (int i = 0; i < found && seen.size() < MAX_NETWORKS; i++) {  // strongest first, one per name
        const String ssid = WiFi.SSID(i);
        bool dup = ssid.isEmpty();
        for (const String& s : seen) dup = dup || s == ssid;
        if (dup) continue;
        seen.push_back(ssid);
        sendResult(improv::GET_WIFI_NETWORKS,
                   {ssid, String(WiFi.RSSI(i)), WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "NO" : "YES"});
      }
      WiFi.scanDelete();
      sendResult(improv::GET_WIFI_NETWORKS, {});  // an empty result ends the list
      break;
    }
    case improv::WIFI_SETTINGS:
      // Try the network before saving it: a typo must not replace Wi-Fi that works.
      joinSsid = cmd.ssid.c_str();
      joinPass = cmd.password.c_str();
      joining = true;
      joinStartedAt = millis();
      sendState(improv::STATE_PROVISIONING);
      WiFi.disconnect();
      WiFi.begin(joinSsid.c_str(), joinPass.c_str());
      break;
    case improv::BAD_CHECKSUM:
      sendError(improv::ERROR_INVALID_RPC);
      break;
    default:
      sendError(improv::ERROR_UNKNOWN_RPC);
      break;
  }
}

void pollJoin() {
  if (!joining) return;
  if (WiFi.status() == WL_CONNECTED) {
    joining = false;
    settings::setWifi(joinSsid, joinPass);
    if (hooks.wifiChanged) hooks.wifiChanged();
    sendState(improv::STATE_PROVISIONED);
    sendResult(improv::WIFI_SETTINGS, {});
  } else if (millis() - joinStartedAt > JOIN_TIMEOUT_MS) {
    joining = false;
    sendError(improv::ERROR_UNABLE_TO_CONNECT);
    sendState(improv::STATE_AUTHORIZED);
    WiFi.disconnect();
    const auto& c = settings::get();
    if (settings::hasWifi()) WiFi.begin(c.ssid.c_str(), c.pass.c_str());  // back to what worked
  }
}

// ---- kai-config lines ----

void reply(JsonDocument& doc) {
  String out;
  serializeJson(doc, out);
  Serial.print(LINE_PREFIX);
  Serial.println(out);
}

void handleConfig(const String& json) {
  JsonDocument in, out;
  if (deserializeJson(in, json) || !in.is<JsonObject>()) {
    out["ok"] = false;
    out["error"] = "bad json";
    reply(out);
    return;
  }
  bool ok = true, relayChanged = false;
  String error;
  if (in["relay"].is<const char*>()) {
    if (settings::setRelay(in["relay"].as<const char*>())) relayChanged = true;
    else ok = false, error = "bad relay address";
  }
  if (in["key"].is<const char*>()) {
    std::string id, hex;
    if (provutil::parseKeyLine(in["key"].as<const char*>(), id, hex) && settings::setKey(id.c_str(), hex.c_str())) {
      relayChanged = true;
    } else {
      ok = false, error = "bad key (expected kai-xxxx:<64 hex>)";
    }
  }
  if (in["unpair"] | false) {  // forget the device key (and legacy token): pair again on the next connect
    settings::clearKey();
    settings::clearToken();
    relayChanged = true;
  }
  if (in["reset"] | false) {
    settings::factoryReset();
    out["ok"] = true;
    out["reset"] = true;
    reply(out);
    delay(200);
    ESP.restart();
  }
  const auto& c = settings::get();
  out["ok"] = ok;
  if (!ok) out["error"] = error;
  out["device"] = settings::deviceName();
  out["fw"] = fwVersion;
  out["wifi"] = c.ssid;
  out["wifi_connected"] = WiFi.status() == WL_CONNECTED;
  out["relay"] = c.relayHost.length() ? c.relayHost + ":" + c.relayPort : String("");  // set by hand
  out["found"] = c.foundHost.length() ? c.foundHost + ":" + c.foundPort : String("");  // on the network
  out["key"] = settings::hasKey() ? c.keyId : String("");  // the identity only, never the key
  out["pairing"] = pairing::status();
  reply(out);
  if (relayChanged && hooks.relayChanged) hooks.relayChanged();
}

void feedLine(uint8_t b) {
  if (b == '\n' || b == '\r') {
    if (line.startsWith(LINE_PREFIX)) handleConfig(line.substring(strlen(LINE_PREFIX)));
    line = "";
  } else if (line.length() < 512 && b >= 0x20 && b < 0x7f) {
    line += char(b);
  } else if (b < 0x20 || b >= 0x7f) {
    line = "";  // binary (an Improv frame): not a config line
  }
}

}  // namespace

void begin(const char* firmwareVersion, Hooks h) {
  fwVersion = firmwareVersion;
  hooks = h;
}

void poll() {
  while (Serial.available()) {
    const uint8_t b = Serial.read();
    feedLine(b);
    const bool keep = improv::parse_improv_serial_byte(
        framePos, b, frameBuf,
        [](improv::ImprovCommand cmd) {
          command = cmd;
          haveCommand = true;
          return false;  // the frame is complete: start over
        },
        [](improv::Error err) { sendError(err); });
    if (keep && framePos < sizeof(frameBuf)) frameBuf[framePos++] = b;
    else framePos = 0;
    if (haveCommand) {
      haveCommand = false;
      handleImprov(command);
    }
  }
  pollJoin();
}

bool busy() { return joining; }

}  // namespace provision
