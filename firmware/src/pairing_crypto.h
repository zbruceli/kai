// The Stick's half of pairing with a code (no Arduino, so it's tested on the host: pio test -e native).
// Mirrors relay/kai_relay/pairing.py, which documents the protocol; a shared test vector keeps them equal.
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace paircrypto {

constexpr size_t PUB_BYTES = 32, NONCE_BYTES = 16, KEY_BYTES = 32, COMMIT_BYTES = 64;

void publicKey(const uint8_t priv[32], uint8_t pub[32]);

// SHA512("kai-pair-v1 commit" PR PS NR): the relay's promise about its nonce, sent before it sees ours.
void commitment(const uint8_t relayPub[32], const uint8_t stickPub[32], const uint8_t relayNonce[16],
                uint8_t out[64]);

// Constant-time comparison of a received commitment with the one we compute.
bool commitmentMatches(const uint8_t relayPub[32], const uint8_t stickPub[32], const uint8_t relayNonce[16],
                       const uint8_t received[64]);

// The device key and the 6-digit code ("215831" + NUL). False if the relay's public key gives an all-zero
// secret (a low-order point), which would make the key predictable.
bool derive(const uint8_t ownPriv[32], const uint8_t theirPub[32], const char* identity, const uint8_t stickPub[32],
            const uint8_t relayPub[32], const uint8_t stickNonce[16], const uint8_t relayNonce[16], uint8_t key[32],
            char code[7]);

// Hex helpers for the JSON messages.
bool fromHex(const char* hex, uint8_t* out, size_t len);  // exactly 2*len hex characters
void toHex(const uint8_t* in, size_t len, char* out);     // out: 2*len + 1

}  // namespace paircrypto
