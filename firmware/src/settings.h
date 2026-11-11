// Per-device settings, stored in NVS (flash that survives reflashing; a full erase or a factory reset
// clears it). The firmware binary itself holds no secrets, so one build serves every Stick.
//
// Set up over USB (see provision.h): Wi-Fi via Improv (the web flasher page), the rest with a
// `kai-config` line. Developer builds with a firmware/src/secrets.h seed these on first boot.
#pragma once
#include <Arduino.h>

namespace settings {

struct Config {
  String ssid, pass;          // home Wi-Fi
  String relayHost;           // the relay's address, set by hand; empty means "find it on the network"
  uint16_t relayPort = 8443;  // its TLS-PSK port
  String foundHost;           // the relay found on the network (mDNS `_kai._tcp`); a manual address wins
  uint16_t foundPort = 8443;
  String keyId, keyHex;       // this Stick's TLS-PSK identity and 32-byte key (64 hex characters)
  String token;               // legacy shared token: only for a relay without device keys (plain ws://)
  bool keyApproved = true;    // false for a freshly paired key until the relay first accepts it
};

void begin();               // load; seeds from secrets.h on the very first boot of a developer build
const Config& get();
String deviceName();        // "kai-xxxx" from the MAC, the same on every boot

bool hasWifi();
bool hasKey();

void setWifi(const String& ssid, const String& pass);
bool setRelay(const String& hostPort);                    // "host" or "host:port"; "" clears it
void setFound(const String& host, uint16_t port);         // remember the relay found on the network
// approved=false for a key from pairing: it isn't known to work until the relay accepts it once.
bool setKey(const String& identity, const String& hex, bool approved = true);  // false if malformed
bool keyApproved();         // the relay has accepted this key at least once
void setKeyApproved();
void clearKey();            // forget the device key (a pairing the relay never approved)
void clearToken();          // forget the legacy shared token
void factoryReset();        // erase everything (stays erased: secrets.h is not re-applied)

}  // namespace settings
