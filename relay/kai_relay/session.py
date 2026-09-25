"""Bridges one Stick WebSocket to one Gemini Live session.

Device -> relay
    text   {"type": "hello", "device": str, "battery": int, "fw": str}
    text   {"type": "ptt_start"}            button A pressed (also barges in on Kai speaking)
    binary PCM16 mono 16 kHz mic audio      only between ptt_start and ptt_end
    text   {"type": "ptt_end"}              button A released
Relay -> device
    text   {"type": "state", "state": "idle" | "thinking"}
    text   {"type": "card", "title": str, "lines": [str], "icon"?: "mic"|"weather"|"fishing"|"camera", "mood"?: "happy"}
    text   {"type": "caption", "delta": str} next words of what Kai is saying (ASCII, for the screen)
    text   {"type": "interrupted"}           drop any queued speech
    text   {"type": "turn_complete"}
    binary PCM16 mono 24 kHz speech, <= DEVICE_FRAME bytes per frame
"""

import asyncio
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

from . import tools
from .config import Settings
from .persona import system_prompt

log = logging.getLogger("kai.session")

IN_RATE = 16000
# Presses shorter than this are taps, not questions: nothing is sent to Gemini.
MIN_UTTERANCE_BYTES = IN_RATE * 2 * 300 // 1000
DEVICE_FRAME = 4096
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
        self._last_activity = time.monotonic()

    # ---- device side -------------------------------------------------------

    async def run(self) -> None:
        idle = asyncio.create_task(self._idle_watchdog())
        try:
            async for msg in self.ws:
                self._last_activity = time.monotonic()
                if isinstance(msg, bytes):
                    await self._on_mic(msg)
                else:
                    await self._on_control(json.loads(msg))
        except ConnectionClosed:
            pass
        finally:
            idle.cancel()
            await self._close_live()
            log.info("[%s] device disconnected", self.device)

    async def _on_control(self, m: dict) -> None:
        kind = m.get("type")
        if kind == "hello":
            self.device = m.get("device", "?")
            log.info("[%s] hello fw=%s battery=%s%%", self.device, m.get("fw"), m.get("battery"))
            await self._send({"type": "state", "state": "idle"})
        elif kind == "ptt_start":
            self._ptt = True
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
        try:
            await self.ws.send(json.dumps(obj))
        except ConnectionClosed:
            pass

    async def _send_audio(self, pcm: bytes) -> None:
        try:
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
            for r in results:
                if r.card:
                    await self._send({
                        **r.card,
                        "type": "card",
                        "title": screen_text(r.card["title"]),
                        "lines": [screen_text(line) for line in r.card["lines"]],
                    })
            await live.send_tool_response(
                function_responses=[
                    types.FunctionResponse(id=fc.id, name=fc.name, response=r.data) for fc, r in zip(calls, results)
                ]
            )

        sc = msg.server_content
        if not sc:
            return
        if sc.interrupted:
            await self._send({"type": "interrupted"})
        if sc.input_transcription and sc.input_transcription.text:
            self._heard += sc.input_transcription.text
        # While the button is held, anything Kai was still saying is stale.
        if sc.model_turn and not self._ptt:
            for part in sc.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    self._audio_bytes += len(part.inline_data.data)
                    await self._send_audio(part.inline_data.data)
        if sc.output_transcription and sc.output_transcription.text and not self._ptt:
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
            self._heard = self._caption = ""
            self._audio_bytes = 0
            await self._send({"type": "turn_complete"})

    async def _idle_watchdog(self) -> None:
        """Drop the Gemini session when unused: resets context and stops paying for a growing history."""
        while True:
            await asyncio.sleep(15)
            if self._live and not self._ptt and time.monotonic() - self._last_activity > self.settings.idle_close_s:
                log.info("[%s] idle for %ss", self.device, self.settings.idle_close_s)
                await self._close_live()
