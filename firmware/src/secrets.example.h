// Copy to secrets.h (git-ignored) and fill in.
#pragma once

#define WIFI_SSID "your-ssid"
#define WIFI_PASS "your-password"

// The home server running kai-relay: its LAN IP (reserve it in your router), or a "*.local" name if the
// server runs mDNS (avahi).
#define RELAY_HOST "192.168.1.50"
#define RELAY_PORT 8765       // plain ws://, only used without a device key below
#define RELAY_TLS_PORT 8443   // the encrypted link (KAI_TLS_PORT on the relay)

// The encrypted link: this device's pre-shared key. Make one on the relay with `uv run kai-psk kai-xxxx`,
// which prints these two lines and the matching KAI_DEVICE_PSKS entry for relay/.env. Without them the
// Stick falls back to unencrypted ws://, which the relay refuses once it has device keys.
#define KAI_PSK_IDENTITY "kai-xxxx"
#define KAI_PSK_HEX "0000000000000000000000000000000000000000000000000000000000000000"

// Must match KAI_DEVICE_TOKEN in relay/.env
#define KAI_DEVICE_TOKEN "change-me"
