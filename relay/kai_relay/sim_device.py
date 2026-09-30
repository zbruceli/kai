"""Pretend to be the Stick from your Mac: Enter to start talking, Enter to stop.

    uv run --extra sim kai-sim --host raspberrypi.local

Speaks the same WebSocket protocol as the firmware, so the relay can be tested end-to-end before flashing.
With KAI_SIM_PSK=identity:hexkey (a key from kai-psk, also listed in the relay's KAI_DEVICE_PSKS) it uses the
encrypted link on KAI_TLS_PORT, like a Stick.
"""

import argparse
import asyncio
import json
import os
import threading

import sounddevice as sd
from dotenv import load_dotenv
from websockets.asyncio.client import connect

from . import tls

IN_RATE, OUT_RATE, BLOCK = 16000, 24000, 512


class Speaker:
    """Jitter-free playback of whatever PCM arrives, fed from the network thread."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream = sd.RawOutputStream(samplerate=OUT_RATE, channels=1, dtype="int16", callback=self._fill)
        self._stream.start()

    def _fill(self, outdata, frames, _time, _status) -> None:
        n = frames * 2
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        outdata[: len(chunk)] = chunk
        outdata[len(chunk) :] = b"\x00" * (n - len(chunk))

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            self._buf.extend(pcm)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


async def run(host: str, port: int, token: str, psk: str = "") -> None:
    loop = asyncio.get_running_loop()
    mic_q: asyncio.Queue[bytes] = asyncio.Queue()
    speaker = Speaker()
    talking = False

    def on_mic(indata, _frames, _time, _status) -> None:
        if talking:
            loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))

    mic = sd.RawInputStream(samplerate=IN_RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=on_mic)
    mic.start()

    tls_ctx = None
    if psk:  # the encrypted link, like a Stick with a device key
        identity, key = next(iter(tls.parse_psks(psk).items()))
        tls_ctx = tls.client_context(identity, key)
    uri = f"{'wss' if tls_ctx else 'ws'}://{host}:{port}/ws"
    async with connect(uri, additional_headers={"Authorization": f"Bearer {token}"}, max_size=2**20,
                       ssl=tls_ctx) as ws:
        await ws.send(json.dumps({"type": "hello", "device": "kai-sim", "fw": "sim", "battery": 100}))
        print(f"Connected to {uri}. Press Enter to talk, Enter again to send. Ctrl-C quits.")

        async def receive() -> None:
            async for msg in ws:
                if isinstance(msg, bytes):
                    if not talking:
                        speaker.feed(msg)
                    continue
                m = json.loads(msg)
                kind = m["type"]
                if kind == "card":
                    print(f"\n┌ {m['title']}\n" + "\n".join(f"│ {line}" for line in m["lines"]) + "\n└")
                elif kind == "caption":
                    print(m["delta"], end="", flush=True)
                elif kind == "interrupted":
                    speaker.clear()
                elif kind == "state":
                    print(f"\n[{m['state']}]")
                elif kind == "turn_complete":
                    print("\n[done]")

        async def send_mic() -> None:
            while True:
                await ws.send(await mic_q.get())

        async def keyboard() -> None:
            nonlocal talking
            while True:
                await loop.run_in_executor(None, input)
                if not talking:
                    speaker.clear()  # barge in
                    await ws.send(json.dumps({"type": "ptt_start"}))
                    talking = True
                    print("[listening… press Enter to send]")
                else:
                    talking = False
                    await asyncio.sleep(0.05)  # let the last mic block flush
                    await ws.send(json.dumps({"type": "ptt_end"}))

        await asyncio.gather(receive(), send_mic(), keyboard())


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    psk = os.environ.get("KAI_SIM_PSK", "")
    p.add_argument("--port", type=int, default=int(os.environ.get("KAI_TLS_PORT" if psk else "KAI_PORT",
                                                                  "8443" if psk else "8765")))
    p.add_argument("--token", default=os.environ.get("KAI_DEVICE_TOKEN", ""))
    p.add_argument("--mic", help="input device name or index (see --list-devices)")
    p.add_argument("--speaker", help="output device name or index")
    p.add_argument("--list-devices", action="store_true")
    args = p.parse_args()
    if args.list_devices:
        print(sd.query_devices())
        return
    as_device = lambda v: int(v) if v and v.isdigit() else v  # noqa: E731
    sd.default.device = (as_device(args.mic), as_device(args.speaker))
    try:
        asyncio.run(run(args.host, args.port, args.token, psk))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
