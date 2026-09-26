#include "audio.h"

#include <esp_heap_caps.h>
#include <math.h>

namespace audio {
namespace {

// ---- mic: three buffers in rotation. M5.Mic holds two requests at a time, so once a third is queued
// the oldest one is guaranteed complete and can be sent.
int16_t micBuf[3][MIC_CHUNK];
size_t recIdx = 0;
size_t queued = 0;

// ---- speaker: a PSRAM ring absorbs network bursts (Gemini sends faster than real time).
constexpr size_t RING_BYTES = 2 * 1024 * 1024;               // ~43 s of 24 kHz speech
constexpr size_t PREBUFFER_BYTES = SPEAKER_RATE * 2 / 5;     // 200 ms before starting playback
constexpr size_t PLAY_CHUNK = 1200;                          // samples per playRaw (50 ms)
uint8_t* ring = nullptr;
size_t ringHead = 0, ringTail = 0, ringCount = 0;
int16_t playBuf[3][PLAY_CHUNK];  // three in sequence, per M5Unified's playRaw() contract
size_t playIdx = 0;
bool playing = false;

float lastLevel = 0;
uint32_t underruns = 0;

constexpr uint8_t ES8311_ADDR = 0x18;

// ---- loudness: Gemini's voice is fairly quiet for a 1 W speaker, so boost it digitally, but scale the
// boost per chunk so peaks never clip. Hardware volume is maxed on USB; on battery M5 advises staying
// under ~75% because loud peaks can brown out the small cell.
constexpr uint8_t VOLUME_USB = 255;
constexpr uint8_t VOLUME_BATTERY = 200;
constexpr float MAX_GAIN = 3.0f;
constexpr float PEAK_TARGET = 30000.0f;
float gain = MAX_GAIN;

void boost(int16_t* s, size_t n) {
  int peak = 1;
  for (size_t i = 0; i < n; i++) peak = max(peak, abs(int(s[i])));
  // Drop gain instantly on loud chunks, recover slowly so quiet syllables don't pump.
  float want = min(MAX_GAIN, PEAK_TARGET / peak);
  gain = want < gain ? want : gain + (want - gain) * 0.05f;
  for (size_t i = 0; i < n; i++) {
    int v = int(s[i] * gain);
    s[i] = int16_t(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
  }
}


float rms(const int16_t* s, size_t n) {
  if (n == 0) return 0;
  double acc = 0;
  for (size_t i = 0; i < n; i++) acc += double(s[i]) * s[i];
  float v = sqrtf(acc / n) / 6000.0f;  // speech sits well below full scale
  return v > 1 ? 1 : v;
}

size_t ringRead(uint8_t* dst, size_t n) {
  n = min(n, ringCount);
  size_t first = min(n, RING_BYTES - ringTail);
  memcpy(dst, ring + ringTail, first);
  memcpy(dst + first, ring, n - first);
  ringTail = (ringTail + n) % RING_BYTES;
  ringCount -= n;
  return n;
}

}  // namespace

// M5Unified's speaker end() only switches the amp off and leaves the ES8311 DAC running, so power the
// codec down ourselves (the same sequence M5Unified uses when the mic stops). Both begin() calls
// reinitialise it.
void codecPowerDown() {
  M5.In_I2C.writeRegister8(ES8311_ADDR, 0x0D, 0xFC, 100000);  // analog circuitry off
  M5.In_I2C.writeRegister8(ES8311_ADDR, 0x0E, 0x6A, 100000);
  M5.In_I2C.writeRegister8(ES8311_ADDR, 0x00, 0x00, 100000);  // CSM power down
}

bool begin() {
  ring = static_cast<uint8_t*>(heap_caps_malloc(RING_BYTES, MALLOC_CAP_SPIRAM));
  M5.Mic.end();
  M5.Speaker.end();
  codecPowerDown();  // nothing plays until Kai has something to say
  updateVolume();
  return ring != nullptr;
}

void speakerOn() {
  if (M5.Speaker.isRunning()) return;
  M5.Speaker.begin();
  updateVolume();
}

void powerDown() {
  clearPlayback();
  if (M5.Speaker.isRunning()) M5.Speaker.end();
  if (M5.Mic.isRunning()) M5.Mic.end();
  codecPowerDown();
}

void startMic() {
  clearPlayback();
  M5.Speaker.end();
  M5.Mic.begin();
  recIdx = 0;
  queued = 0;
}

void pollMic(MicSink sink) {
  if (!M5.Mic.record(micBuf[recIdx], MIC_CHUNK, MIC_RATE)) return;
  queued++;
  if (queued >= 3) {
    const int16_t* done = micBuf[(recIdx + 1) % 3];  // the buffer queued two calls ago
    lastLevel = rms(done, MIC_CHUNK);
    sink(done, MIC_CHUNK);
  }
  recIdx = (recIdx + 1) % 3;
}

void stopMic(MicSink sink) {
  while (M5.Mic.isRecording()) delay(1);
  // Up to two recorded buffers haven't been sent yet: recIdx-2 then recIdx-1.
  if (queued >= 2) sink(micBuf[(recIdx + 1) % 3], MIC_CHUNK);
  if (queued >= 1) sink(micBuf[(recIdx + 2) % 3], MIC_CHUNK);
  M5.Mic.end();  // also powers the codec down; the speaker starts when a reply arrives
  lastLevel = 0;
}

void enqueue(const uint8_t* pcm, size_t bytes) {
  if (!ring) return;
  speakerOn();
  if (bytes > RING_BYTES - ringCount) {
    log_w("speaker ring full, dropping %u bytes", bytes);
    bytes = RING_BYTES - ringCount;
  }
  size_t first = min(bytes, RING_BYTES - ringHead);
  memcpy(ring + ringHead, pcm, first);
  memcpy(ring, pcm + first, bytes - first);
  ringHead = (ringHead + bytes) % RING_BYTES;
  ringCount += bytes;
}

void pollSpeaker(bool turnComplete) {
  if (!playing) {
    if (ringCount >= PREBUFFER_BYTES || (turnComplete && ringCount > 0)) {
      playing = true;
    } else {
      return;
    }
  }
  while (ringCount >= 2 && M5.Speaker.isPlaying(0) < 2) {
    size_t n = ringRead(reinterpret_cast<uint8_t*>(playBuf[playIdx]), min(ringCount & ~size_t(1), PLAY_CHUNK * 2)) / 2;
    boost(playBuf[playIdx], n);
    lastLevel = rms(playBuf[playIdx], n) / 2;  // boosted audio would peg the mouth otherwise
    M5.Speaker.playRaw(playBuf[playIdx], n, SPEAKER_RATE, false, 1, 0);
    playIdx = (playIdx + 1) % 3;
  }
  if (ringCount < 2 && !M5.Speaker.isPlaying(0)) {
    if (!turnComplete) underruns++;
    playing = false;  // underrun or finished: prebuffer again before resuming
    lastLevel = 0;
  }
}

void clearPlayback() {
  if (M5.Speaker.isRunning()) M5.Speaker.stop();
  ringHead = ringTail = ringCount = 0;
  playing = false;
  lastLevel = 0;
}

bool playbackIdle() { return ringCount < 2 && (!M5.Speaker.isRunning() || !M5.Speaker.isPlaying()); }

float level() { return lastLevel; }

uint32_t bufferedMs() { return ringCount / (SPEAKER_RATE * 2 / 1000); }

uint32_t takeUnderruns() {
  const uint32_t n = underruns;
  underruns = 0;
  return n;
}

bool onUsbPower() {
  return M5.Power.isCharging() == m5::Power_Class::is_charging || M5.Power.getVBUSVoltage() > 4000;
}

void updateVolume() { M5.Speaker.setVolume(onUsbPower() ? VOLUME_USB : VOLUME_BATTERY); }

}  // namespace audio
