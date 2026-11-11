#include "settings.h"

#include <Preferences.h>
#include <esp_mac.h>

#include "provision_util.h"

#if __has_include("secrets.h")
#include "secrets.h"
#define KAI_HAVE_SECRETS 1
#endif

namespace settings {
namespace {

constexpr const char* NS = "kai";
Config cfg;

Preferences prefs;

void load() {
  prefs.begin(NS, true);
  cfg.ssid = prefs.getString("ssid", "");
  cfg.pass = prefs.getString("pass", "");
  cfg.relayHost = prefs.getString("relay", "");
  cfg.relayPort = prefs.getUShort("port", provutil::DEFAULT_RELAY_PORT);
  cfg.foundHost = prefs.getString("found", "");
  cfg.foundPort = prefs.getUShort("fport", provutil::DEFAULT_RELAY_PORT);
  cfg.keyId = prefs.getString("key_id", "");
  cfg.keyHex = prefs.getString("key", "");
  cfg.token = prefs.getString("token", "");
  prefs.end();
}

// A developer build carries firmware/src/secrets.h: copy its real values (not the example's
// placeholders) into NVS once, so an existing Stick keeps working after moving to this firmware.
void seedFromSecrets() {
  prefs.begin(NS, false);
  if (prefs.getBool("seeded", false)) {
    prefs.end();
    return;
  }
#ifdef KAI_HAVE_SECRETS
#ifdef WIFI_SSID
  if (String(WIFI_SSID) != "your-ssid" && String(WIFI_SSID).length()) {
    prefs.putString("ssid", WIFI_SSID);
    prefs.putString("pass", WIFI_PASS);
  }
#endif
#ifdef RELAY_HOST
  if (String(RELAY_HOST) != "192.168.1.50") prefs.putString("relay", RELAY_HOST);
#endif
#ifdef RELAY_TLS_PORT
  prefs.putUShort("port", RELAY_TLS_PORT);
#endif
#if defined(KAI_PSK_IDENTITY) && defined(KAI_PSK_HEX)
  if (provutil::validIdentity(KAI_PSK_IDENTITY) && provutil::validKeyHex(KAI_PSK_HEX)) {
    prefs.putString("key_id", KAI_PSK_IDENTITY);
    prefs.putString("key", KAI_PSK_HEX);
  }
#endif
#ifdef KAI_DEVICE_TOKEN
  if (String(KAI_DEVICE_TOKEN) != "change-me") prefs.putString("token", KAI_DEVICE_TOKEN);
#endif
  log_i("settings seeded from secrets.h");
#endif
  prefs.putBool("seeded", true);  // only ever once, so a factory reset really empties the Stick
  prefs.end();
}

}  // namespace

void begin() {
  seedFromSecrets();
  load();
}

const Config& get() { return cfg; }

String deviceName() {
  uint8_t mac[6] = {};
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char name[12];
  snprintf(name, sizeof(name), "kai-%02X%02X", mac[4], mac[5]);
  return name;
}

bool hasWifi() { return cfg.ssid.length() > 0; }

bool hasKey() { return cfg.keyId.length() > 0 && cfg.keyHex.length() > 0; }

void setWifi(const String& ssid, const String& pass) {
  prefs.begin(NS, false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.end();
  cfg.ssid = ssid;
  cfg.pass = pass;
}

bool setRelay(const String& hostPort) {
  std::string host;
  uint16_t port;
  if (!provutil::parseHostPort(hostPort.c_str(), host, port)) return false;
  prefs.begin(NS, false);
  prefs.putString("relay", host.c_str());
  prefs.putUShort("port", port);
  prefs.end();
  cfg.relayHost = host.c_str();
  cfg.relayPort = port;
  return true;
}

void setFound(const String& host, uint16_t port) {
  if (host == cfg.foundHost && port == cfg.foundPort) return;  // spare the flash
  prefs.begin(NS, false);
  prefs.putString("found", host);
  prefs.putUShort("fport", port);
  prefs.end();
  cfg.foundHost = host;
  cfg.foundPort = port;
}

bool setKey(const String& identity, const String& hex) {
  if (!provutil::validIdentity(identity.c_str()) || !provutil::validKeyHex(hex.c_str())) return false;
  prefs.begin(NS, false);
  prefs.putString("key_id", identity);
  prefs.putString("key", hex);
  prefs.end();
  cfg.keyId = identity;
  cfg.keyHex = hex;
  return true;
}

void clearKey() {
  prefs.begin(NS, false);
  prefs.remove("key_id");
  prefs.remove("key");
  prefs.end();
  cfg.keyId = cfg.keyHex = "";
}

void clearToken() {
  prefs.begin(NS, false);
  prefs.remove("token");
  prefs.end();
  cfg.token = "";
}

void factoryReset() {
  prefs.begin(NS, false);
  prefs.clear();
  prefs.putBool("seeded", true);
  prefs.end();
  cfg = Config{};
}

}  // namespace settings
