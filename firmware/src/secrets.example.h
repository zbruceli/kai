// Copy to secrets.h (git-ignored) and fill in.
#pragma once

#define WIFI_SSID "your-ssid"
#define WIFI_PASS "your-password"

// The home server running kai-relay: its LAN IP (reserve it in your router), or a "*.local" name if the
// server runs mDNS (avahi).
#define RELAY_HOST "192.168.1.50"
#define RELAY_PORT 8765

// Must match KAI_DEVICE_TOKEN in relay/.env
#define KAI_DEVICE_TOKEN "change-me"
