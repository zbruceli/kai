#include "voice_codec.h"

#include <opus.h>

namespace voice {
namespace {

constexpr int UP_RATE = 16000;
constexpr int DOWN_RATE = 24000;
constexpr int UP_BITRATE = 24000;   // speech at 16 kHz: ~11x smaller than PCM, still transcribes cleanly
constexpr int UP_COMPLEXITY = 3;    // fixed-point SILK stays well inside real time on the S3 at this level
constexpr int MAX_DOWN_SAMPLES = DOWN_RATE * 120 / 1000;  // the longest Opus packet (120 ms)

OpusEncoder* enc = nullptr;
OpusDecoder* dec = nullptr;
int16_t pcmOut[MAX_DOWN_SAMPLES];

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
  return err == OPUS_OK;
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

void decodeMessage(const uint8_t* msg, size_t len, PcmSink sink) {
  if (!dec || len < 1 || msg[0] != KIND_OPUS) return;
  size_t i = 1;
  while (i + 2 <= len) {
    const size_t n = msg[i] | (msg[i + 1] << 8);
    i += 2;
    if (n == 0 || i + n > len) return;  // truncated: drop the rest of this message
    const uint32_t t0 = micros();
    const int samples = opus_decode(dec, msg + i, n, pcmOut, MAX_DOWN_SAMPLES, 0);
    decUs += micros() - t0;
    decFrames++;
    if (samples > 0) sink(pcmOut, samples);
    i += n;
  }
}

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
