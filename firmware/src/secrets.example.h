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

// The encrypted link: this device's pre-shared key. Normally leave these as they are: a Stick without a key
// pairs with the relay and shows a code to approve (`uv run kai-approve <code>`). To set one by hand, make it
// with `uv run kai-psk kai-xxxx`, which prints these two lines and the KAI_DEVICE_PSKS entry for relay/.env.
#define KAI_PSK_IDENTITY "kai-xxxx"
#define KAI_PSK_HEX "0000000000000000000000000000000000000000000000000000000000000000"

// Legacy, for plain ws:// only: must match KAI_DEVICE_TOKEN in relay/.env. Paired Sticks don't use it.
#define KAI_DEVICE_TOKEN "change-me"
