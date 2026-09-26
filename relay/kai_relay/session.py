"""Bridges one Stick WebSocket to one Gemini Live session.

Device -> relay
    text   {"type": "hello", "device": str, "battery": int, "fw": str, "codecs"?: ["opus"], "sleep"?: {...}}
    text   {"type": "power", ...}   measurement firmware only (docs/POWER.md): logged to data/power.csv
    text   {"type": "ptt_start"}            button A pressed (also barges in on Kai speaking)
    binary mic audio, only between ptt_start and ptt_end:
           with "opus" in hello: 0x02 + Opus bundle, 16 kHz mono 20 ms packets (see opus.py)
           otherwise: raw PCM16 mono 16 kHz (older firmware, kai-sim)
    text   {"type": "ptt_end"}              button A released
Relay -> device
    text   {"type": "state", "state": "idle" | "thinking"}
    text   {"type": "card", "title": str, "lines": [str], "icon"?: "mic"|"weather"|"fishing"|"camera", "mood"?: "happy"}
    text   {"type": "caption", "delta": str} next words of what Kai is saying (ASCII, for the screen)
    text   {"type": "interrupted"}           drop any queued speech
    text   {"type": "turn_complete"}
    binary Kai's speech: 0x02 + Opus bundle at 24 kHz if the device asked for opus, else raw PCM16
           mono 24 kHz; <= DEVICE_FRAME bytes per message
"""

import asyncio
import contextlib
import json
import logging
import time
import unicodedata
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any

from google import genai
from google.genai import types
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from . import backstop, opus, tools
from .power import PowerLog, sleep_estimate_ma
from .config import Settings
from .persona import system_prompt

log = logging.getLogger("kai.session")

IN_RATE = 16000
# Presses shorter than this are taps, not questions: nothing is sent to Gemini.
MIN_UTTERANCE_BYTES = IN_RATE * 2 * 300 // 1000
DEVICE_FRAME = 4096
OPUS_DOWN_BITRATE = 32000  # Kai's voice, 24 kHz mono; mic is encoded on the Stick
OUT_BYTES_PER_S = 24000 * 2

# The Stick's fonts are ASCII-only: map common typography, then strip accents ("Año Nuevo" -> "Ano Nuevo").
_ASCII = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-",
                        "\u2014": "-", "\u2026": "...", "\u00b0": "", "\u00a0": " "})


def screen_text(text: str) -> str:
    return unicodedata.normalize("NFKD", text.translate(_ASCII)).encode("ascii", "ignore").decode()


class KaiSession:
    def __init__(self, ws: ServerConnection, client: genai.Client, ctx: tools.ToolContext, settings: Settings):
        self.ws = ws
        self.client = client
        self.ctx = ctx
        self.settings = settings
        self.device = "?"
        self.power = PowerLog(settings.data_dir)

        self._live: Any = None  # google.genai AsyncSession
        self._stack: AsyncExitStack | None = None
        self._recv_task: asyncio.Task | None = None

        self._ptt = False
        self._activity_open = False
        self._pending: list[bytes] = []
        self._pending_bytes = 0
        self._caption = ""
        self._heard = ""
        self._audio_bytes = 0
        # Per utterance (one button press): what was said, and whether a card reached the screen.
        self._utterance = ""
        self._card_sent = False
        self._last_place: str | None = None
        self._last_tool: str | None = None
        self._utterance_id = 0
        self._tasks: set[asyncio.Task] = set()
        self._opus_up: opus.Decoder | None = None    # set when the device's hello asks for Opus
        self._opus_down: opus.Encoder | None = None
        self._model_busy = False  # Gemini is mid-answer
        self._drop_turn = False   # user hushed it with a tap: discard until turn_complete
        self._last_activity = time.monotonic()

    # ---- device side -------------------------------------------------------

    async def run(self) -> None:
        idle = asyncio.create_task(self._idle_watchdog())
        try:
            async for msg in self.ws:
                self._last_activity = time.monotonic()
                if isinstance(msg, bytes):
                    await self._on_mic(self._decode_mic(msg))
                    continue
                try:
                    control = json.loads(msg)
                except ValueError:
                    control = None
                if isinstance(control, dict):
                    await self._on_control(control)
                else:
                    log.warning("[%s] ignoring malformed message", self.device)
        except ConnectionClosed:
            pass
        finally:
            idle.cancel()
            for task in self._tasks:
                task.cancel()
            await self._close_live()
            log.info("[%s] device disconnected", self.device)

    async def _on_control(self, m: dict) -> None:
        kind = m.get("type")
        if kind == "hello":
            self.device = m.get("device", "?")
            if "opus" in (m.get("codecs") or []):
                self._opus_up = opus.Decoder(16000)
                self._opus_down = opus.Encoder(24000, OPUS_DOWN_BITRATE)
            # No reply needed: the device shows idle itself, and a "state: idle" here would knock it out of
            # Thinking when it replays speech captured while waking from deep sleep.
            log.info("[%s] hello fw=%s battery=%s%% audio=%s", self.device, m.get("fw"), m.get("battery"),
                     "opus" if self._opus_down else "pcm")
            if isinstance(m.get("sleep"), dict):
                sl = m["sleep"]
                self.power.write(self.device, "sleep", **sl)
                ma = sleep_estimate_ma(sl.get("slept_s", 0), sl.get("mv_before", 0), sl.get("mv_after", 0))
                log.info("[%s] woke after %.1f h asleep, %s -> %s mV%s", self.device, sl.get("slept_s", 0) / 3600,
                         sl.get("mv_before"), sl.get("mv_after"), f" (~{ma:.2f} mA)" if ma is not None else "")
        elif kind == "power":
            self.power.write(self.device, "report", **{k: v for k, v in m.items() if k != "type"})
        elif kind == "ptt_start":
            self._ptt = True
            self._utterance_id += 1
            if self._opus_down:
                self._opus_down.reset()  # anything Kai was still saying is stale
                self._opus_up = opus.Decoder(16000)
            self._utterance = ""
            self._card_sent = False
            self._activity_open = False
            self._pending.clear()
            self._pending_bytes = 0
            try:
                await self._ensure_live()  # connect now so it's ready by the time they finish talking
            except Exception:
                log.exception("[%s] couldn't open Gemini Live", self.device)
                await self._error("Can't reach Gemini")
        elif kind == "ptt_end":
            self._ptt = False
            if self._activity_open and self._live:
                self._activity_open = False
                await self._send({"type": "state", "state": "thinking"})
                await self._safe_live(self._live.send_realtime_input(activity_end=types.ActivityEnd()))
            else:
                # A tap, too short to send. The device already stopped Kai talking; if Gemini is still
                # generating, drop the rest of that answer instead of resuming it mid-sentence.
                if self._model_busy:
                    self._drop_turn = True
                await self._send({"type": "state", "state": "idle"})
        else:
            log.debug("[%s] ignoring %s", self.device, m)

    async def _on_mic(self, chunk: bytes) -> None:
        if not self._ptt or not self._live:
            return
        if self._activity_open:
            await self._safe_live(self._send_mic(chunk))
            return
        self._pending.append(chunk)
        self._pending_bytes += len(chunk)
        if self._pending_bytes >= MIN_UTTERANCE_BYTES:
            self._activity_open = True
            buffered, self._pending = self._pending, []
            await self._safe_live(self._start_utterance(buffered))

    async def _start_utterance(self, buffered: list[bytes]) -> None:
        # activity_start also interrupts Kai if it's still talking.
        await self._live.send_realtime_input(activity_start=types.ActivityStart())
        await self._send_mic(b"".join(buffered))

    async def _send_mic(self, pcm: bytes) -> None:
        await self._live.send_realtime_input(audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={IN_RATE}"))

    async def _send(self, obj: dict) -> None:
        with contextlib.suppress(ConnectionClosed):
            await self.ws.send(json.dumps(obj))

    def _decode_mic(self, msg: bytes) -> bytes:
        """Mic audio as PCM16 16 kHz, whatever the device sent."""
        if not self._opus_up:
            return msg
        if not msg or msg[0] != opus.KIND_OPUS:
            log.warning("[%s] unexpected binary frame kind", self.device)
            return b""
        return b"".join(self._opus_up.decode(p) for p in opus.unbundle(msg))

    async def _send_audio(self, pcm: bytes, final: bool = False) -> None:
        try:
            if self._opus_down:
                packets = self._opus_down.encode(pcm) + (self._opus_down.flush() if final else [])
                for message in opus.bundle(packets):
                    await self.ws.send(message)
                return
            for i in range(0, len(pcm), DEVICE_FRAME):
                await self.ws.send(pcm[i : i + DEVICE_FRAME])
        except ConnectionClosed:
            pass

    async def _error(self, text: str) -> None:
        await self._send({"type": "card", "title": "Oops", "lines": [text]})
        await self._send({"type": "state", "state": "idle"})

    # ---- Gemini side -------------------------------------------------------

    def _live_config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig.model_validate({
            "response_modalities": ["AUDIO"],
            "system_instruction": system_prompt(self.settings, datetime.now()),
            "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": self.settings.voice}}},
            "tools": [{"google_search": {}}, {"function_declarations": tools.declarations()}],
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            # Push-to-talk: the button marks turns, not Gemini's voice activity detection.
            "realtime_input_config": {"automatic_activity_detection": {"disabled": True}},
        })

    async def _ensure_live(self) -> None:
        if self._live is not None:
            return
        stack = AsyncExitStack()
        live = await stack.enter_async_context(
            self.client.aio.live.connect(model=self.settings.model, config=self._live_config())
        )
        self._live, self._stack = live, stack
        self._recv_task = asyncio.create_task(self._receive_loop(live))
        log.info("[%s] Gemini Live session opened (%s)", self.device, self.settings.model)

    async def _close_live(self) -> None:
        live, stack, task = self._live, self._stack, self._recv_task
        self._live = self._stack = self._recv_task = None
        self._activity_open = False
        if task and task is not asyncio.current_task():
            task.cancel()
        if stack:
            try:
                await stack.aclose()
            except Exception:
                log.debug("error closing Gemini session", exc_info=True)
        if live:
            log.info("[%s] Gemini Live session closed", self.device)

    async def _safe_live(self, coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("[%s] Gemini send failed; will reconnect on next press", self.device)
            await self._close_live()
            await self._error("Lost Gemini, try again")

    async def _receive_loop(self, live) -> None:
        try:
            while True:
                got_any = False
                # receive() ends after each turn_complete, so keep re-entering it.
                async for msg in live.receive():
                    got_any = True
                    await self._on_live_message(live, msg)
                if not got_any:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] Gemini receive loop ended", self.device)
        if self._live is live:
            await self._close_live()

    async def _on_live_message(self, live, msg) -> None:
        if msg.go_away:
            log.info("[%s] Gemini will close this session soon (time left %s)", self.device, msg.go_away.time_left)

        if msg.tool_call:
            await self._send({"type": "state", "state": "thinking"})
            calls = msg.tool_call.function_calls or []
            results = await asyncio.gather(*(tools.call(fc.name, fc.args or {}, self.ctx) for fc in calls))
            for fc, r in zip(calls, results, strict=True):
                if (fc.args or {}).get("place"):
                    self._last_place = fc.args["place"]
                if r.card:
                    self._last_tool = fc.name
                    await self._send_card(r.card)
            await live.send_tool_response(
                function_responses=[
                    types.FunctionResponse(id=fc.id, name=fc.name, response=r.data) for fc, r in zip(calls, results, strict=True)
                ]
            )

        sc = msg.server_content
        if not sc:
            return
        if sc.interrupted:
            await self._send({"type": "interrupted"})
        if sc.input_transcription and sc.input_transcription.text:
            self._heard += sc.input_transcription.text
            self._utterance += sc.input_transcription.text
        if sc.model_turn:
            self._model_busy = True
        # While the button is held, or after a hushing tap, anything Kai was still saying is stale.
        mute = self._ptt or self._drop_turn
        if sc.model_turn and not mute:
            for part in sc.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    self._audio_bytes += len(part.inline_data.data)
                    await self._send_audio(part.inline_data.data)
        if sc.output_transcription and sc.output_transcription.text and not mute:
            delta = sc.output_transcription.text
            self._caption += delta
            await self._send({"type": "caption", "delta": screen_text(delta)})
        if sc.turn_complete:
            if self._heard.strip():
                log.info("[%s] you: %s", self.device, self._heard.strip())
            speech_s = self._audio_bytes / OUT_BYTES_PER_S
            log.info("[%s] kai (%.1fs audio): %s", self.device, speech_s, self._caption.strip() or "-")
            if self._caption.strip() and not self._audio_bytes:
                log.warning("[%s] turn had a transcript but no audio", self.device)
            self._model_busy = self._drop_turn = False
            if self._audio_bytes and not self._card_sent:
                self._card_sent = True  # at most one backstop per utterance
                task = asyncio.create_task(self._backstop_card(self._utterance, self._utterance_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            self._heard = self._caption = ""
            self._audio_bytes = 0
            if self._opus_down:
                await self._send_audio(b"", final=True)  # the last partial Opus frame
            await self._send({"type": "turn_complete"})

    async def _send_card(self, card: dict) -> None:
        self._card_sent = True
        await self._send({
            **card,
            "type": "card",
            "title": screen_text(card["title"]),
            "lines": [screen_text(line) for line in card["lines"]],
        })

    async def _backstop_card(self, said: str, utterance_id: int) -> None:
        """Gemini answered without a tool; if the question clearly wanted one, run it so a card appears."""
        inferred = backstop.infer_tool_call(said, self._last_place, self._last_tool)
        if not inferred:
            return
        name, args = inferred
        log.info("[%s] backstop: Gemini skipped the tool, running %s(%s)", self.device, name, args)
        result = await tools.call(name, args, self.ctx)
        if utterance_id != self._utterance_id:
            return  # the user has asked something else since; this card would be stale
        if result.card and "error" not in result.data:
            if args.get("place"):
                self._last_place = args["place"]
            self._last_tool = name
            await self._send_card(result.card)

    async def _idle_watchdog(self) -> None:
        """Drop the Gemini session when unused: resets context and stops paying for a growing history."""
        while True:
            await asyncio.sleep(15)
            if self._live and not self._ptt and time.monotonic() - self._last_activity > self.settings.idle_close_s:
                log.info("[%s] idle for %ss", self.device, self.settings.idle_close_s)
                await self._close_live()
