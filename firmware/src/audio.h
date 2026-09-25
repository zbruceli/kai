// Half-duplex audio for the StickS3: the ES8311 codec runs either the mic or the speaker, never both.
#pragma once
#include <M5Unified.h>

namespace audio {

constexpr uint32_t MIC_RATE = 16000;    // what Gemini Live expects
constexpr uint32_t SPEAKER_RATE = 24000;  // what Gemini Live produces
constexpr size_t MIC_CHUNK = 512;       // samples per frame sent to the relay (32 ms)

using MicSink = void (*)(const int16_t* samples, size_t count);

bool begin();

// Mic side: startMic() switches the codec to capture; pollMic() streams finished chunks to `sink`.
void startMic();
void pollMic(MicSink sink);
void stopMic(MicSink sink);  // flushes the tail, then switches back to the speaker

// Speaker side: queue PCM16 from the relay; pollSpeaker() keeps the speaker fed.
void enqueue(const uint8_t* pcm, size_t bytes);
void pollSpeaker(bool turnComplete);
void clearPlayback();
bool playbackIdle();

// Max volume on USB power, a little lower on battery (loud peaks can brown out the 250 mAh cell).
void updateVolume();
bool onUsbPower();

// 0..1 loudness of the most recent chunk, for the face's mouth and the mic meter.
float level();

}  // namespace audio
