#include "voice_codec.h"

#include <esp_heap_caps.h>
#include <opus.h>

namespace voice {
namespace {

constexpr int UP_RATE = 16000;
constexpr int DOWN_RATE = 24000;
constexpr int UP_BITRATE = 24000;   // speech at 16 kHz: ~11x smaller than PCM, still transcribes cleanly
// Measured on the StickS3 at 240 MHz per 20 ms frame: complexity 0 = 5.9 ms, 1 = 6.4 ms, 2 = 8.0 ms,
// 3 = 8.6 ms. Level 1 still transcribes word for word through Gemini, at ~25% less CPU than 3.
constexpr int UP_COMPLEXITY = 1;
constexpr int MAX_DOWN_SAMPLES = DOWN_RATE * 120 / 1000;  // the longest Opus packet (120 ms)

OpusEncoder* enc = nullptr;
OpusDecoder* dec = nullptr;
int16_t pcmOut[MAX_DOWN_SAMPLES];

// Compressed downlink queue: [u16 length][packet] entries in a PSRAM ring (~64 s at 32 kbit/s; answers
// arrive ~4x faster than they play, so a long answer can queue most of its length).
constexpr size_t QUEUE_BYTES = 256 * 1024;
constexpr size_t MAX_PACKET = 1276;  // largest legal Opus packet
uint8_t* queue = nullptr;
size_t qHead = 0, qTail = 0, qCount = 0;
uint8_t packet[MAX_PACKET];

void qWrite(const uint8_t* src, size_t n) {
  const size_t first = min(n, QUEUE_BYTES - qHead);
  memcpy(queue + qHead, src, first);
  memcpy(queue, src + first, n - first);
  qHead = (qHead + n) % QUEUE_BYTES;
  qCount += n;
}

void qRead(uint8_t* dst, size_t n) {
  const size_t first = min(n, QUEUE_BYTES - qTail);
  memcpy(dst, queue + qTail, first);
  memcpy(dst + first, queue, n - first);
  qTail = (qTail + n) % QUEUE_BYTES;
  qCount -= n;
}

uint32_t encUs = 0, encFrames = 0, decUs = 0, decFrames = 0;

}  // namespace

bool begin() {
  int err = OPUS_OK;
  enc = opus_encoder_create(UP_RATE, 1, OPUS_APPLICATION_VOIP, &err);
  if (err != OPUS_OK) return false;
  opus_encoder_ctl(enc, OPUS_SET_BITRATE(UP_BITRATE));
  opus_encoder_ctl(enc, OPUS_SET_COMPLEXITY(UP_COMPLEXITY));
  opus_encoder_ctl(enc, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
  dec = opus_decoder_create(DOWN_RATE, 1, &err);
  queue = static_cast<uint8_t*>(heap_caps_malloc(QUEUE_BYTES, MALLOC_CAP_SPIRAM));
  return err == OPUS_OK && queue;
}

void resetEncoder() {
  if (enc) opus_encoder_ctl(enc, OPUS_RESET_STATE);
}

void resetDecoder() {
  if (dec) opus_decoder_ctl(dec, OPUS_RESET_STATE);
}

size_t encodeEntry(const int16_t* pcm, uint8_t* dst, size_t cap) {
  if (!enc || cap < MAX_ENTRY) return 0;
  const uint32_t t0 = micros();
  const int n = opus_encode(enc, pcm, UP_FRAME, dst + 2, MAX_ENTRY - 2);
  encUs += micros() - t0;
  encFrames++;
  if (n <= 0) return 0;
  dst[0] = n & 0xff;
  dst[1] = n >> 8;
  return n + 2;
}

bool queueMessage(const uint8_t* msg, size_t len) {
  if (!queue || len < 1 || msg[0] != KIND_OPUS) return false;
  size_t i = 1;
  while (i + 2 <= len) {
    const size_t n = msg[i] | (msg[i + 1] << 8);
    if (n == 0 || n > MAX_PACKET || i + 2 + n > len) return false;  // truncated: drop the rest
    if (qCount + 2 + n > QUEUE_BYTES) return false;                // full (~16 s ahead): drop
    qWrite(msg + i, 2 + n);
    i += 2 + n;
  }
  return true;
}

bool decodeNext(PcmSink sink) {
  if (!dec || qCount < 2) return false;
  uint8_t lenBytes[2];
  qRead(lenBytes, 2);
  const size_t n = lenBytes[0] | (lenBytes[1] << 8);
  qRead(packet, n);
  const uint32_t t0 = micros();
  const int samples = opus_decode(dec, packet, n, pcmOut, MAX_DOWN_SAMPLES, 0);
  decUs += micros() - t0;
  decFrames++;
  if (samples > 0) sink(pcmOut, samples);
  return true;
}

bool pending() { return qCount >= 2; }

void dropQueued() { qHead = qTail = qCount = 0; }

uint32_t takeEncodeUs() {
  const uint32_t avg = encFrames ? encUs / encFrames : 0;
  encUs = encFrames = 0;
  return avg;
}

uint32_t takeDecodeUs() {
  const uint32_t avg = decFrames ? decUs / decFrames : 0;
  decUs = decFrames = 0;
  return avg;
}

}  // namespace voice
