// Setting up a Stick over USB serial, so one firmware build serves every Stick:
//   - Improv Wi-Fi (https://www.improv-wifi.com/): the web flasher page (ESP Web Tools) asks for the
//     Wi-Fi name and password after flashing and sends them this way. The Stick saves them only once it
//     has actually joined that network.
//   - `kai-config {json}` lines for everything else, answered with `kai-config {json}`:
//       {"get": true}                    -> device name, firmware, Wi-Fi name, relay, whether a key is set
//       {"relay": "host[:port]"}         -> the relay's address ("" clears it: find it on the network)
//       {"key": "kai-xxxx:<64 hex>"}     -> this Stick's device key, as `kai-psk` prints it
//       {"unpair": true}                 -> forget the device key: pair with the relay again (with a code)
//       {"reset": true}                  -> factory reset
//     Secrets are never sent back.
#pragma once
#include <Arduino.h>

namespace provision {

struct Hooks {
  void (*wifiChanged)();   // new Wi-Fi saved: forget cached access points
  void (*relayChanged)();  // relay address or key changed: reconnect
};

void begin(const char* firmwareVersion, Hooks hooks);
void poll();   // call every loop
bool busy();   // an Improv Wi-Fi attempt is in progress: leave Wi-Fi alone

}  // namespace provision
