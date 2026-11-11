// Host-side tests for setting up a Stick (src/provision_util.h): pio test -e native
#include <improv.h>
#include <unity.h>

#include <string>
#include <vector>

#include "provision_util.h"

using namespace provutil;

void setUp() {}
void tearDown() {}

void test_relay_address() {
  std::string host;
  uint16_t port;
  TEST_ASSERT_TRUE(parseHostPort("192.168.1.50", host, port));
  TEST_ASSERT_EQUAL_STRING("192.168.1.50", host.c_str());
  TEST_ASSERT_EQUAL_UINT16(DEFAULT_RELAY_PORT, port);
  TEST_ASSERT_TRUE(parseHostPort("kai.local:9443", host, port));
  TEST_ASSERT_EQUAL_STRING("kai.local", host.c_str());
  TEST_ASSERT_EQUAL_UINT16(9443, port);
  TEST_ASSERT_TRUE(parseHostPort("", host, port));  // clears it
  TEST_ASSERT_TRUE(host.empty());
  for (const char* bad : {"host:", ":8443", "host:0", "host:70000", "host:84a3", "a:b:c", "my relay", "h/x"}) {
    TEST_ASSERT_FALSE_MESSAGE(parseHostPort(bad, host, port), bad);
  }
}

void test_device_key() {
  const std::string hex(64, 'a');
  std::string id, key;
  TEST_ASSERT_TRUE(parseKeyLine("kai-4514:" + hex, id, key));
  TEST_ASSERT_EQUAL_STRING("kai-4514", id.c_str());
  TEST_ASSERT_FALSE(parseKeyLine("kai-4514:" + std::string(64, '0'), id, key));  // the example's placeholder
  TEST_ASSERT_FALSE(parseKeyLine("kai-4514:" + std::string(63, 'a'), id, key));  // too short
  TEST_ASSERT_FALSE(parseKeyLine("kai-4514:" + std::string(63, 'a') + "g", id, key));
  TEST_ASSERT_FALSE(parseKeyLine("kai 4514:" + hex, id, key));                    // bad identity
  TEST_ASSERT_FALSE(parseKeyLine(hex, id, key));                                  // no identity
}

// What the web page sends is an Improv WIFI_SETTINGS RPC. Frame it with our code and check the Improv SDK's
// own parser reads it back: the same framing we use for our replies.
void test_improv_frame_round_trip() {
  const std::string ssid = "Home Wi-Fi", pass = "correct horse";
  std::vector<uint8_t> rpc = {improv::WIFI_SETTINGS, 0, uint8_t(ssid.size())};
  rpc.insert(rpc.end(), ssid.begin(), ssid.end());
  rpc.push_back(uint8_t(pass.size()));
  rpc.insert(rpc.end(), pass.begin(), pass.end());
  rpc[1] = uint8_t(rpc.size() - 2);

  uint8_t frame[300];
  const size_t n = improvFrame(improv::IMPROV_SERIAL_VERSION, improv::TYPE_RPC, rpc.data(), rpc.size(), frame);
  TEST_ASSERT_EQUAL_UINT(rpc.size() + 10, n);

  uint8_t buf[300];
  bool got = false;
  improv::ImprovCommand cmd{};
  for (size_t i = 0; i < n; i++) {
    const bool keep = improv::parse_improv_serial_byte(
        i, frame[i], buf,
        [&](improv::ImprovCommand c) {
          cmd = c;
          got = true;
          return false;
        },
        [](improv::Error) { TEST_FAIL_MESSAGE("parser reported an error"); });
    if (keep) buf[i] = frame[i];
    else TEST_ASSERT_EQUAL_UINT(n - 1, i);  // only the final byte ends the frame
  }
  TEST_ASSERT_TRUE(got);
  TEST_ASSERT_EQUAL(improv::WIFI_SETTINGS, cmd.command);
  TEST_ASSERT_EQUAL_STRING(ssid.c_str(), cmd.ssid.c_str());
  TEST_ASSERT_EQUAL_STRING(pass.c_str(), cmd.password.c_str());
}

void test_improv_bad_checksum_is_rejected() {
  const uint8_t rpc[] = {improv::GET_DEVICE_INFO, 0};
  uint8_t frame[32], buf[32];
  const size_t n = improvFrame(improv::IMPROV_SERIAL_VERSION, improv::TYPE_RPC, rpc, sizeof(rpc), frame);
  frame[n - 1] ^= 0xFF;  // corrupt the checksum
  bool errored = false;
  for (size_t i = 0; i < n; i++) {
    const bool keep = improv::parse_improv_serial_byte(
        i, frame[i], buf, [](improv::ImprovCommand) { return true; },
        [&](improv::Error e) { errored = e == improv::ERROR_INVALID_RPC; });
    if (keep) buf[i] = frame[i];
  }
  TEST_ASSERT_TRUE(errored);
}

void test_manual_relay_wins_over_discovered() {
  RelayTarget t;
  TEST_ASSERT_TRUE(chooseRelay("192.168.1.50", 9443, "192.168.1.77", 8443, t));
  TEST_ASSERT_EQUAL_STRING("192.168.1.50", t.host.c_str());
  TEST_ASSERT_EQUAL_UINT16(9443, t.port);
  TEST_ASSERT_FALSE(t.discovered);
  TEST_ASSERT_TRUE(chooseRelay("", 8443, "192.168.1.77", 8443, t));  // nothing set by hand: use the found one
  TEST_ASSERT_EQUAL_STRING("192.168.1.77", t.host.c_str());
  TEST_ASSERT_TRUE(t.discovered);
  TEST_ASSERT_FALSE(chooseRelay("", 8443, "", 8443, t));             // neither: go and look
}

void test_pick_announced_relay() {
  const Announced found[] = {
      {"", 8443, true},               // no IPv4 address: skipped
      {"192.168.1.60", 8765, false},  // the plain listener
      {"192.168.1.50", 8443, true},   // the encrypted link
  };
  TEST_ASSERT_EQUAL_INT(2, pickAnnounced(found, 3, true));   // with a device key: only tls=1
  TEST_ASSERT_EQUAL_INT(1, pickAnnounced(found, 3, false));  // legacy token only: the plain one
  TEST_ASSERT_EQUAL_INT(-1, pickAnnounced(found, 1, true));
  TEST_ASSERT_EQUAL_INT(-1, pickAnnounced(found, 0, true));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_relay_address);
  RUN_TEST(test_device_key);
  RUN_TEST(test_improv_frame_round_trip);
  RUN_TEST(test_improv_bad_checksum_is_rejected);
  RUN_TEST(test_manual_relay_wins_over_discovered);
  RUN_TEST(test_pick_announced_relay);
  return UNITY_END();
}
