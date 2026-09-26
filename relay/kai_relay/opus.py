"""Opus framing and codecs for the Stick link.

Binary WebSocket messages from Opus-capable firmware start with a one-byte kind:
    0x02  Opus bundle: repeated [u16 little-endian length][Opus packet], 20 ms packets
Uplink packets are 16 kHz mono (the Stick's mic), downlink packets are 24 kHz mono (Gemini's voice).
Firmware that doesn't advertise "opus" in hello keeps sending raw PCM16 with no header.

Uses PyAV's bundled libopus, so the relay host needs no system codec library.
"""

import fractions
import struct

import av
import numpy as np

KIND_OPUS = 0x02
FRAME_MS = 20
MAX_BUNDLE_BYTES = 4096


def bundle(packets: list[bytes]) -> list[bytes]:
    """Pack packets into as few messages as fit in MAX_BUNDLE_BYTES each."""
    messages, current = [], bytearray([KIND_OPUS])
    for p in packets:
        if len(current) > 1 and len(current) + 2 + len(p) > MAX_BUNDLE_BYTES:
            messages.append(bytes(current))
            current = bytearray([KIND_OPUS])
        current += struct.pack("<H", len(p)) + p
    if len(current) > 1:
        messages.append(bytes(current))
    return messages


def unbundle(message: bytes) -> list[bytes]:
    """Packets in one Opus bundle message (header byte already checked by the caller)."""
    packets, i = [], 1
    while i + 2 <= len(message):
        (n,) = struct.unpack_from("<H", message, i)
        i += 2
        if n == 0 or i + n > len(message):
            break  # truncated or corrupt: drop the rest of this message
        packets.append(message[i:i + n])
        i += n
    return packets


class Decoder:
    """Opus packets -> PCM16 mono bytes at `rate` (libopus decodes at 48 kHz; we resample)."""

    def __init__(self, rate: int):
        self._codec = av.CodecContext.create("libopus", "r")
        self._codec.sample_rate = rate
        self._codec.layout = "mono"
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)

    def decode(self, packet: bytes) -> bytes:
        out = bytearray()
        try:
            frames = self._codec.decode(av.Packet(packet))
        except av.error.InvalidDataError:
            return b""  # a corrupt packet costs 20 ms of audio, nothing more
        for frame in frames:
            for res in self._resampler.resample(frame):
                out += res.to_ndarray().astype(np.int16).tobytes()
        return bytes(out)


class Encoder:
    """PCM16 mono bytes at `rate` -> 20 ms Opus packets. Buffers partial frames between calls."""

    def __init__(self, rate: int, bitrate: int):
        self.rate = rate
        self.frame_samples = rate * FRAME_MS // 1000
        self._bitrate = bitrate
        self._pending = bytearray()
        self._open()

    def _open(self) -> None:
        c = av.CodecContext.create("libopus", "w")
        c.sample_rate = self.rate
        c.layout = "mono"
        c.format = "s16"
        c.bit_rate = self._bitrate
        c.time_base = fractions.Fraction(1, self.rate)
        c.options = {"application": "voip", "frame_duration": str(FRAME_MS)}
        c.open()
        self._codec, self._pts = c, 0

    def encode(self, pcm: bytes) -> list[bytes]:
        self._pending += pcm
        frame_bytes = self.frame_samples * 2
        packets = []
        while len(self._pending) >= frame_bytes:
            chunk, self._pending = bytes(self._pending[:frame_bytes]), self._pending[frame_bytes:]
            packets += self._encode_frame(chunk)
        return packets

    def flush(self) -> list[bytes]:
        """Encode what's left, zero-padded to a whole frame (end of a turn)."""
        if not self._pending:
            return []
        chunk = bytes(self._pending).ljust(self.frame_samples * 2, b"\0")
        self._pending.clear()
        return self._encode_frame(chunk)

    def reset(self) -> None:
        """Drop buffered audio and codec state (an interrupted answer)."""
        self._pending.clear()
        self._open()

    def _encode_frame(self, chunk: bytes) -> list[bytes]:
        samples = np.frombuffer(chunk, dtype=np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = self.rate
        frame.pts = self._pts
        self._pts += self.frame_samples
        return [bytes(p) for p in self._codec.encode(frame)]
