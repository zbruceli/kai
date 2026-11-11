// node docs/flash/improv.test.mjs: checks the page's Improv framing against what the firmware sends and expects.
import assert from "node:assert/strict";
import { CMD, Splitter, TYPE, compareVersions, resultStrings, rpc, wifiSettings } from "./improv.js";

const bytes = s => Array.from(new TextEncoder().encode(s));
const withSum = arr => [...arr, arr.reduce((a, b) => a + b, 0) & 0xff];

// A request, byte for byte (the layout firmware/src/provision_util.h improvFrame() builds and parses).
assert.deepEqual(Array.from(rpc(CMD.GET_INFO)), withSum([...bytes("IMPROV"), 1, 0x03, 2, 0x03, 0]));
assert.deepEqual(Array.from(wifiSettings("Home", "pässword")),
  withSum([...bytes("IMPROV"), 1, 0x03, 2 + 1 + 4 + 1 + 9, 0x01, 1 + 4 + 1 + 9, 4, ...bytes("Home"), 9, ...bytes("pässword")]));

// The Stick's replies, mixed with log text and split across chunks, as they arrive over USB.
const result = withSum([...bytes("IMPROV"), 1, TYPE.RESULT, 2 + 1 + 3 + 1 + 3, CMD.GET_NETWORKS, 1 + 3 + 1 + 3, 3, ...bytes("Net"), 3, ...bytes("-60")]);
const state = withSum([...bytes("IMPROV"), 1, TYPE.STATE, 1, 0x02]);
const stream = [...bytes("boot I am Kai\n"), ...result, 10, ...bytes("kai-config {\"ok\":true}\n"), ...state, 10];
for (const cut of [1, 5, 9, 12, 20, stream.length - 3]) {
  const s = new Splitter();
  const a = s.push(new Uint8Array(stream.slice(0, cut)));
  const b = s.push(new Uint8Array(stream.slice(cut)));
  const frames = [...a.frames, ...b.frames];
  assert.equal(frames.length, 2, `cut at ${cut}`);
  assert.equal(frames[0].type, TYPE.RESULT);
  assert.deepEqual(resultStrings(frames[0].data), ["Net", "-60"]);
  assert.deepEqual(frames[1], { type: TYPE.STATE, data: [0x02] });
  assert.equal(a.text + b.text, "boot I am Kai\n\nkai-config {\"ok\":true}\n\n");
}

// A bad checksum is text, not a frame; an "I" in plain text passes through.
const bad = [...state.slice(0, -1), (state.at(-1) + 1) & 0xff];
const out = new Splitter().push(new Uint8Array([...bytes("Is it? "), ...bad, ...bytes("ok\n")]));
assert.equal(out.frames.length, 0);
assert.ok(out.text.startsWith("Is it? IMPROV") && out.text.endsWith("ok\n"));

assert.equal(compareVersions("0.13.10", "0.13.2"), 1);
assert.equal(compareVersions("0.13.2", "0.13.2"), 0);
assert.equal(compareVersions("0.12.9", "0.13.0"), -1);

console.log("improv.js: all checks passed");
