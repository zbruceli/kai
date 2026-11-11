// The Stick's pairing crypto against the shared test vector in relay/tests/test_pairing.py: both sides must
// agree byte for byte, or a real Stick and relay would compute different keys and codes.
#include <unity.h>

#include <string.h>

#include "pairing_crypto.h"

using namespace paircrypto;

static uint8_t stickPriv[32], relayPriv[32], ns[16], nr[16];

void setUp() {
  for (int i = 0; i < 32; i++) stickPriv[i] = uint8_t(1 + i), relayPriv[i] = uint8_t(33 + i);
  for (int i = 0; i < 16; i++) ns[i] = uint8_t(0xA0 + i), nr[i] = uint8_t(0xB0 + i);
}
void tearDown() {}

static void hex(const uint8_t* b, size_t n, char* out) { toHex(b, n, out); }

void test_matches_the_relay_vector() {
  uint8_t ps[32], pr[32], commit[64], key[32];
  char h[129], code[7];
  publicKey(stickPriv, ps);
  publicKey(relayPriv, pr);
  hex(ps, 32, h);
  TEST_ASSERT_EQUAL_STRING("07a37cbc142093c8b755dc1b10e86cb426374ad16aa853ed0bdfc0b2b86d1c7c", h);
  hex(pr, 32, h);
  TEST_ASSERT_EQUAL_STRING("5869aff450549732cbaaed5e5df9b30a6da31cb0e5742bad5ad4a1a768f1a67b", h);
  commitment(pr, ps, nr, commit);
  hex(commit, 64, h);
  TEST_ASSERT_EQUAL_STRING(
      "10092636b07f9cf39043b62d05badd8a26ad895a05e99b5c8679ba8d275546f6"
      "7578693ea5cfdc8f4a131e519e5ebc4c0e16c27b403df0a2c5e3f9b3b80bf8e8", h);
  TEST_ASSERT_TRUE(derive(stickPriv, pr, "kai-test", ps, pr, ns, nr, key, code));
  hex(key, 32, h);
  TEST_ASSERT_EQUAL_STRING("e4fa513e8a9f8c3743b04239eeafcd839e0707792e789b171a5a01dcdaf628a4", h);
  TEST_ASSERT_EQUAL_STRING("215831", code);
}

void test_commitment_check_catches_a_changed_nonce() {
  uint8_t ps[32], pr[32], commit[64];
  publicKey(stickPriv, ps);
  publicKey(relayPriv, pr);
  commitment(pr, ps, nr, commit);
  TEST_ASSERT_TRUE(commitmentMatches(pr, ps, nr, commit));
  uint8_t other[16];
  memcpy(other, nr, 16);
  other[0] ^= 1;  // the relay (or someone in between) changed its nonce after committing
  TEST_ASSERT_FALSE(commitmentMatches(pr, ps, other, commit));
}

void test_low_order_point_is_refused() {
  uint8_t ps[32], zero[32] = {0}, key[32];
  char code[7];
  publicKey(stickPriv, ps);
  TEST_ASSERT_FALSE(derive(stickPriv, zero, "kai-test", ps, zero, ns, nr, key, code));
}

void test_key_is_bound_to_the_device_name() {
  uint8_t ps[32], pr[32], a[32], b[32];
  char ca[7], cb[7];
  publicKey(stickPriv, ps);
  publicKey(relayPriv, pr);
  derive(stickPriv, pr, "kai-test", ps, pr, ns, nr, a, ca);
  derive(stickPriv, pr, "kai-other", ps, pr, ns, nr, b, cb);
  TEST_ASSERT_FALSE(memcmp(a, b, 32) == 0);
}

void test_hex() {
  uint8_t out[2];
  TEST_ASSERT_TRUE(fromHex("aB0f", out, 2));
  TEST_ASSERT_EQUAL_UINT8(0xab, out[0]);
  TEST_ASSERT_EQUAL_UINT8(0x0f, out[1]);
  TEST_ASSERT_FALSE(fromHex("abc", out, 2));   // wrong length
  TEST_ASSERT_FALSE(fromHex("zz00", out, 2));  // not hex
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_matches_the_relay_vector);
  RUN_TEST(test_commitment_check_catches_a_changed_nonce);
  RUN_TEST(test_low_order_point_is_refused);
  RUN_TEST(test_key_is_bound_to_the_device_name);
  RUN_TEST(test_hex);
  return UNITY_END();
}
