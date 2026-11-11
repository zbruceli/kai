// Pure helpers for setting up a Stick (no Arduino), unit-tested on the host: pio test -e native
#pragma once
#include <stddef.h>
#include <stdint.h>

#include <string>

namespace provutil {

constexpr size_t KEY_BYTES = 32;
constexpr uint16_t DEFAULT_RELAY_PORT = 8443;  // the relay's TLS-PSK port

// "host" or "host:port" -> host and port. Empty input clears the address (host = "", port = default).
// False for anything malformed: spaces, a bad or zero port, more than one colon.
inline bool parseHostPort(const std::string& in, std::string& host, uint16_t& port) {
  host.clear();
  port = DEFAULT_RELAY_PORT;
  if (in.empty()) return true;
  if (in.find_first_of(" \t/") != std::string::npos) return false;
  const size_t colon = in.find(':');
  if (colon == std::string::npos) {
    host = in;
    return true;
  }
  if (in.find(':', colon + 1) != std::string::npos || colon == 0 || colon + 1 == in.size()) return false;
  unsigned long p = 0;
  for (size_t i = colon + 1; i < in.size(); i++) {
    if (in[i] < '0' || in[i] > '9') return false;
    p = p * 10 + (in[i] - '0');
    if (p > 65535) return false;
  }
  if (p == 0) return false;
  host = in.substr(0, colon);
  port = uint16_t(p);
  return true;
}

// A device identity as the relay accepts it: letters, digits, '-' and '_', 1-32 characters.
inline bool validIdentity(const std::string& id) {
  if (id.empty() || id.size() > 32) return false;
  for (char c : id) {
    const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_';
    if (!ok) return false;
  }
  return true;
}

// A 32-byte key as 64 hex characters, and not all zeros (the placeholder in secrets.example.h).
inline bool validKeyHex(const std::string& hex) {
  if (hex.size() != 2 * KEY_BYTES) return false;
  bool nonZero = false;
  for (char c : hex) {
    const bool digit = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
    if (!digit) return false;
    if (c != '0') nonZero = true;
  }
  return nonZero;
}

// "identity:hex", the form `kai-psk` prints for the relay's KAI_DEVICE_PSKS.
inline bool parseKeyLine(const std::string& line, std::string& identity, std::string& hex) {
  const size_t colon = line.find(':');
  if (colon == std::string::npos) return false;
  identity = line.substr(0, colon);
  hex = line.substr(colon + 1);
  return validIdentity(identity) && validKeyHex(hex);
}

// An Improv serial frame: "IMPROV", version, type, length, data, checksum (sum of all bytes before it).
// Writes into out (at least len + 10 bytes) and returns the frame length.
inline size_t improvFrame(uint8_t version, uint8_t type, const uint8_t* data, size_t len, uint8_t* out) {
  const uint8_t header[9] = {'I', 'M', 'P', 'R', 'O', 'V', version, type, uint8_t(len)};
  uint8_t sum = 0;
  size_t n = 0;
  for (uint8_t b : header) {
    out[n++] = b;
    sum += b;
  }
  for (size_t i = 0; i < len; i++) {
    out[n++] = data[i];
    sum += data[i];
  }
  out[n++] = sum;
  return n;
}

}  // namespace provutil
