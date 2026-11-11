#include "pairing_crypto.h"

#include <stdio.h>
#include <string.h>

extern "C" {
#include <monocypher.h>
#include <optional/monocypher-ed25519.h>
}

namespace paircrypto {
namespace {

const char LABEL[] = "kai-pair-v1";

// Hash a sequence of byte strings as one message.
struct Part {
  const void* data;
  size_t len;
};

void sha512(const Part* parts, size_t n, uint8_t out[64]) {
  crypto_sha512_ctx ctx;
  crypto_sha512_init(&ctx);
  for (size_t i = 0; i < n; i++) crypto_sha512_update(&ctx, static_cast<const uint8_t*>(parts[i].data), parts[i].len);
  crypto_sha512_final(&ctx, out);
}

}  // namespace

void publicKey(const uint8_t priv[32], uint8_t pub[32]) { crypto_x25519_public_key(pub, priv); }

void commitment(const uint8_t relayPub[32], const uint8_t stickPub[32], const uint8_t relayNonce[16],
                uint8_t out[64]) {
  const Part parts[] = {{LABEL, strlen(LABEL)}, {" commit", 7}, {relayPub, 32}, {stickPub, 32}, {relayNonce, 16}};
  sha512(parts, 5, out);
}

bool commitmentMatches(const uint8_t relayPub[32], const uint8_t stickPub[32], const uint8_t relayNonce[16],
                       const uint8_t received[64]) {
  uint8_t expected[64];
  commitment(relayPub, stickPub, relayNonce, expected);
  return crypto_verify64(expected, received) == 0;
}

bool derive(const uint8_t ownPriv[32], const uint8_t theirPub[32], const char* identity, const uint8_t stickPub[32],
            const uint8_t relayPub[32], const uint8_t stickNonce[16], const uint8_t relayNonce[16], uint8_t key[32],
            char code[7]) {
  uint8_t shared[32];
  crypto_x25519(shared, ownPriv, theirPub);
  uint8_t acc = 0;
  for (uint8_t b : shared) acc |= b;
  if (acc == 0) {
    crypto_wipe(shared, sizeof(shared));
    return false;
  }
  const size_t idLen = strlen(identity);

  uint8_t salt[32];
  memcpy(salt, stickNonce, 16);
  memcpy(salt + 16, relayNonce, 16);
  uint8_t info[sizeof(LABEL) - 1 + 4 + 64 + 32];  // label + " psk" + identity (<= 32) + both public keys
  size_t n = 0;
  memcpy(info + n, LABEL, strlen(LABEL)), n += strlen(LABEL);
  memcpy(info + n, " psk", 4), n += 4;
  memcpy(info + n, identity, idLen), n += idLen;
  memcpy(info + n, stickPub, 32), n += 32;
  memcpy(info + n, relayPub, 32), n += 32;
  crypto_sha512_hkdf(key, 32, shared, sizeof(shared), salt, sizeof(salt), info, n);
  crypto_wipe(shared, sizeof(shared));

  uint8_t digest[64];
  const Part parts[] = {{LABEL, strlen(LABEL)}, {" code", 5}, {identity, idLen}, {stickPub, 32},
                        {relayPub, 32},         {stickNonce, 16}, {relayNonce, 16}};
  sha512(parts, 7, digest);
  const uint32_t v = (uint32_t(digest[0]) << 24) | (uint32_t(digest[1]) << 16) | (uint32_t(digest[2]) << 8) | digest[3];
  snprintf(code, 7, "%06u", unsigned(v % 1000000));
  return true;
}

bool fromHex(const char* hex, uint8_t* out, size_t len) {
  if (!hex || strlen(hex) != 2 * len) return false;
  for (size_t i = 0; i < len; i++) {
    uint8_t v = 0;
    for (int j = 0; j < 2; j++) {
      const char c = hex[2 * i + j];
      v <<= 4;
      if (c >= '0' && c <= '9') v |= c - '0';
      else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
      else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
      else return false;
    }
    out[i] = v;
  }
  return true;
}

void toHex(const uint8_t* in, size_t len, char* out) {
  static const char digits[] = "0123456789abcdef";
  for (size_t i = 0; i < len; i++) {
    out[2 * i] = digits[in[i] >> 4];
    out[2 * i + 1] = digits[in[i] & 15];
  }
  out[2 * len] = '\0';
}

}  // namespace paircrypto
