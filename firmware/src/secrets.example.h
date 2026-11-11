// Optional, for developers. The firmware keeps its settings in NVS and is normally set up over USB (Wi-Fi
// through the web flasher page, the rest with `kai-config` lines; see src/provision.h). A build that has
// a secrets.h copies these values into NVS once, on its first boot, so a Stick can also be set up this way.
// Copy to secrets.h (git-ignored) and fill in.
#pragma once

#define WIFI_SSID "your-ssid"
#define WIFI_PASS "your-password"

// The relay's address. Leave the example value to let the Stick find the relay on the network (the relay
// announces itself as `_kai._tcp`); set it only where multicast doesn't get through.
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
