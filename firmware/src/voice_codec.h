// Opus for the relay link: the mic is encoded at 16 kHz, Kai's voice arrives as 24 kHz packets.
// Wire format (both directions): a binary WebSocket message is KIND_OPUS followed by one or more
// [u16 little-endian length][Opus packet] entries, each packet 20 ms. See relay/kai_relay/opus.py.
#pragma once
#include <Arduino.h>

namespace voice {

constexpr uint8_t KIND_OPUS = 0x02;
constexpr size_t UP_FRAME = 320;     // 20 ms at 16 kHz: the mic chunk size
constexpr size_t MAX_ENTRY = 2 + 400;  // length prefix + the largest packet we ask the encoder for

bool begin();
void resetEncoder();  // start of each utterance
void resetDecoder();  // start of each answer

// Encode one UP_FRAME of mic audio and append it (length-prefixed) at dst. Returns bytes written, or 0.
size_t encodeEntry(const int16_t* pcm, uint8_t* dst, size_t cap);

// Decode every packet in a downlink message, handing each chunk of 24 kHz PCM to sink.
using PcmSink = void (*)(const int16_t* samples, size_t count);
void decodeMessage(const uint8_t* msg, size_t len, PcmSink sink);

// Average codec cost in microseconds per 20 ms frame since the last call (for telemetry builds).
uint32_t takeEncodeUs();
uint32_t takeDecodeUs();

}  // namespace voice
