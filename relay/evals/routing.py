"""Routing eval: does Gemini Live pick the right path (fast answer, a data tool, the slow path, a time or memory
tool) for what the owner says?

Each case goes to a real Gemini Live session with the relay's own system prompt and tool declarations, as text
(no audio), and the first decision is recorded:
  tool:<name>   Gemini called that tool (several parallel calls count as several names)
  slow          it called ask_agent
  offer         no tool; it answered and offered to dig deeper
  clarify       no tool; it asked the owner something back
  fast          no tool; it just answered (from what it knows or Google Search)

Cases are JSON lines: {"id", "say", "expect": [labels, any of which passes], "history": [[role, text], ...],
"brief", "note"}. Run:
  uv run python evals/routing.py [cases.jsonl ...] [--repeat 3] [--only id,id] [--concurrency 4]
It needs GEMINI_API_KEY (from .env) and makes one Live session per case and repeat.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter, defaultdict
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types

from kai_relay import tools
from kai_relay.config import load_settings
from kai_relay.persona import system_prompt

HERE = Path(__file__).parent
DEFAULT_BRIEF = ("The owner is a photographer and avid angler based on the San Mateo County coast near Half Moon "
                 "Bay. Uses imperial units and America/Los_Angeles time.")
OFFER = re.compile(
    r"\b(?:want|like) me to (?:dig|look|research|find|check|keep)|\bshall i (?:dig|look|research|find|check)|"
    r"\bi can (?:dig|look into|research)|\bdig (?:into|deeper)|\blook into it\b", re.I)
# Transcripts of spoken questions often lose the "?".
CLARIFY = re.compile(r"\b(?:i just need to know|could you tell me|which (?:one|time)|what time would)\b", re.I)
TIMEOUT_S = 40


def decide(calls: list[str], said: str) -> str:
    if "ask_agent" in calls:
        return "slow"
    if calls:
        return "+".join(f"tool:{c}" for c in sorted(set(calls)))
    if OFFER.search(said):
        return "offer"
    if "?" in said or CLARIFY.search(said):
        return "clarify"
    return "fast"


def passes(decision: str, expect: list[str]) -> bool:
    got = set(decision.split("+"))
    return any(e in got for e in expect)


async def run_case(client, settings, case: dict) -> dict:
    config = types.LiveConnectConfig.model_validate({
        "response_modalities": ["AUDIO"],
        "system_instruction": system_prompt(settings, datetime.now(), 0, case.get("brief", DEFAULT_BRIEF)),
        "tools": [{"google_search": {}}, {"function_declarations": tools.declarations(True)}],
        "output_audio_transcription": {},
    })
    turns = [types.Content(role=role, parts=[types.Part(text=text)]) for role, text in case.get("history", [])]
    turns.append(types.Content(role="user", parts=[types.Part(text=case["say"])]))
    calls: list[str] = []
    args: list[dict] = []
    said = ""
    searched = False
    t0 = time.monotonic()
    async with client.aio.live.connect(model=settings.model, config=config) as live:
        await live.send_client_content(turns=turns, turn_complete=True)
        async for msg in live.receive():
            if msg.tool_call:
                for fc in msg.tool_call.function_calls or []:
                    calls.append(fc.name)
                    args.append({fc.name: dict(fc.args or {})})
                break  # the routing decision is made; don't run the tool
            sc = msg.server_content
            if not sc:
                continue
            if sc.grounding_metadata:
                searched = True
            if sc.output_transcription and sc.output_transcription.text:
                said += sc.output_transcription.text
            if sc.turn_complete:
                break
    decision = decide(calls, said)
    return {"id": case["id"], "say": case["say"], "expect": case["expect"], "decision": decision,
            "pass": passes(decision, case["expect"]), "args": args, "said": said.strip(),
            "searched": searched, "s": round(time.monotonic() - t0, 1)}


def load_cases(paths: list[Path]) -> list[dict]:
    cases = []
    for path in paths:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if line and not line.startswith("//"):
                try:
                    cases.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise SystemExit(f"{path}:{n}: {e}") from e
    ids = Counter(c["id"] for c in cases)
    if dupes := [i for i, n in ids.items() if n > 1]:
        raise SystemExit(f"duplicate case ids: {dupes}")
    return cases


def group(expect: list[str]) -> str:
    """Which kind of case it is, for the summary."""
    e = set(expect)
    if e <= {"slow"}:
        return "slow"
    if "slow" in e or "offer" in e:
        return "dig deeper (slow or offer)"
    if e <= {"fast", "clarify"}:
        return "fast"
    return "tool"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cases", nargs="*", type=Path, default=[HERE / "routing_example.jsonl"])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only", default="", help="comma-separated case ids")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--model", default="")
    opts = ap.parse_args()

    settings = load_settings()
    if opts.model:
        settings = replace(settings, model=opts.model)
    cases = load_cases(opts.cases)
    if opts.only:
        keep = set(opts.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    client = genai.Client(api_key=settings.gemini_api_key)
    gate = asyncio.Semaphore(opts.concurrency)

    async def one(case: dict) -> dict:
        async with gate:
            for attempt in range(3):
                try:
                    return await asyncio.wait_for(run_case(client, settings, case), TIMEOUT_S)
                except Exception as e:  # a flaky session shouldn't sink the run
                    err = f"{type(e).__name__}: {e}"
                    await asyncio.sleep(2 * (attempt + 1))
            return {"id": case["id"], "say": case["say"], "expect": case["expect"], "decision": "error",
                    "pass": False, "error": err, "said": "", "args": []}

    jobs = [one(c) for c in cases for _ in range(opts.repeat)]
    results = []
    for i, fut in enumerate(asyncio.as_completed(jobs), 1):
        results.append(await fut)
        print(f"\r{i}/{len(jobs)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    report(cases, results, settings.model)

    out = HERE / "results"
    out.mkdir(exist_ok=True)
    path = out / f"routing-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"model": settings.model, "results": results}, indent=1))
    print(f"\nsaved {path.relative_to(HERE.parent)}")


def report(cases: list[dict], results: list[dict], model: str) -> None:
    by_case = defaultdict(list)
    for r in results:
        by_case[r["id"]].append(r)
    groups: dict[str, list[bool]] = defaultdict(list)
    false_handoffs = misses = errors = 0
    print(f"model {model}, {len(cases)} cases, {len(results)} runs\n")
    for case in cases:
        runs = by_case[case["id"]]
        for r in runs:
            groups[group(case["expect"])].append(r["pass"])
            errors += r["decision"] == "error"
            if r["decision"] == "slow" and "slow" not in case["expect"]:
                false_handoffs += 1
            if group(case["expect"]) != "fast" and not r["pass"] and r["decision"] in ("fast", "clarify"):
                misses += 1
        bad = [r for r in runs if not r["pass"]]
        if bad:
            got = ", ".join(sorted({r["decision"] for r in bad}))
            print(f"FAIL {case['id']} ({len(bad)}/{len(runs)}): expected {'|'.join(case['expect'])}, got {got}")
            print(f"     \"{case['say'][:90]}\"")
            if said := next((r["said"] for r in bad if r.get("said")), ""):
                print(f"     Kai: {said[:140]}")
            if err := next((r.get("error") for r in bad if r.get("error")), ""):
                print(f"     error: {err[:140]}")
    total = [p for ps in groups.values() for p in ps]
    print(f"\naccuracy {sum(total)}/{len(total)} = {100 * sum(total) / max(1, len(total)):.0f}%")
    for name, ps in sorted(groups.items()):
        print(f"  {name:28} {sum(ps)}/{len(ps)}")
    print(f"false hand-offs (slow when not wanted): {false_handoffs}")
    print(f"misses (answered/asked when a tool, slow or offer was wanted): {misses}")
    if errors:
        print(f"errors: {errors}")


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
