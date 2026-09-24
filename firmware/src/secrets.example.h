// Copy to secrets.h (git-ignored) and fill in.
#pragma once

#define WIFI_SSID "your-ssid"
#define WIFI_PASS "your-password"

// The Raspberry Pi running kai-relay. A "*.local" name is resolved over mDNS; a plain IP also works.
#define RELAY_HOST "raspberrypi.local"
#define RELAY_PORT 8765

// Must match KAI_DEVICE_TOKEN in relay/.env
#define KAI_DEVICE_TOKEN "change-me"
