// Pairing a new Stick with its relay, with a code (protocol: relay/kai_relay/pairing.py).
//
// With no device key, the Stick runs a short exchange with the relay's pairing endpoint, then shows a
// 6-digit code. The owner runs `kai-approve <code>` on the relay, which only succeeds if the relay computed
// the same code, then presses the front button: only then does the Stick save the key. Nothing secret
// crosses the network, and a man in the middle would show up as two different codes.
#pragma once
#include <Arduino.h>

namespace pairing {

void start(const String& host, uint16_t port);  // begin an exchange with the relay's pairing endpoint
void poll();                                     // call every loop
bool active();                                   // exchanging, or showing a code
bool showingCode();                              // a code is on screen, waiting for the owner
const char* code();                              // "215831"
bool failed();                                   // the last attempt failed (try again later)
const char* error();
const char* status();  // for `kai-config get`: idle, exchanging, showing code, failed: <why>

uint32_t codeAgeMs();  // how long the code has been on screen
bool confirm();  // the owner pressed the front button: save the key (true) and finish
void cancel();   // the side button, or the code expired

}  // namespace pairing
