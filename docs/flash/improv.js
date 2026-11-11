// Improv Wi-Fi serial framing (https://www.improv-wifi.com/serial/), as the Stick speaks it
// (firmware/src/provision.cpp). Pure functions, so they can be tested without a browser:
// node docs/flash/improv.test.mjs

export const TYPE = { STATE: 0x01, ERROR: 0x02, RPC: 0x03, RESULT: 0x04 };
export const CMD = { WIFI_SETTINGS: 0x01, GET_STATE: 0x02, GET_INFO: 0x03, GET_NETWORKS: 0x04 };
export const STATE = { 0x02: "ready", 0x03: "joining", 0x04: "connected" };
export const ERROR = {
  0x00: null, 0x01: "invalid request", 0x02: "unknown request", 0x03: "couldn't join that network",
  0xfe: "timed out", 0xff: "unknown error",
};

const HEADER = [0x49, 0x4d, 0x50, 0x52, 0x4f, 0x56];  // "IMPROV"
const VERSION = 1;
const utf8 = new TextEncoder();

// An RPC request: command, then each string prefixed by its length.
export function rpc(command, strings = []) {
  const payload = [];
  for (const s of strings) {
    const bytes = utf8.encode(s);
    if (bytes.length > 255) throw new Error("too long");
    payload.push(bytes.length, ...bytes);
  }
  const data = [command, payload.length, ...payload];
  const frame = [...HEADER, VERSION, TYPE.RPC, data.length, ...data];
  frame.push(frame.reduce((a, b) => a + b, 0) & 0xff);
  return new Uint8Array(frame);
}

export const wifiSettings = (ssid, password) => rpc(CMD.WIFI_SETTINGS, [ssid, password]);

// Splits a byte stream into Improv frames and plain text. Feed it chunks as they arrive; it keeps a
// partial frame for the next chunk. Returns { frames: [{type, data}], text: "..." }.
export class Splitter {
  constructor() { this.buf = new Uint8Array(0); }

  push(chunk) {
    const buf = new Uint8Array(this.buf.length + chunk.length);
    buf.set(this.buf);
    buf.set(chunk, this.buf.length);
    const frames = [];
    let text = [];
    let i = 0;
    while (i < buf.length) {
      if (buf[i] !== HEADER[0]) { text.push(buf[i++]); continue; }
      // Could be a frame: wait for enough bytes to tell.
      const avail = buf.length - i;
      const headerMatch = HEADER.every((b, k) => k >= avail || buf[i + k] === b);
      if (!headerMatch) { text.push(buf[i++]); continue; }
      if (avail < 9) break;  // need version, type and length
      const len = buf[i + 8];
      if (avail < 10 + len) break;  // the rest hasn't arrived
      const frame = buf.subarray(i, i + 10 + len);
      const sum = frame.subarray(0, 9 + len).reduce((a, b) => a + b, 0) & 0xff;
      if (frame[6] === VERSION && sum === frame[9 + len]) {
        frames.push({ type: frame[7], data: Array.from(frame.subarray(9, 9 + len)) });
        i += 10 + len;
      } else {
        text.push(buf[i++]);  // looked like a header, wasn't one
      }
    }
    this.buf = buf.slice(i);
    return { frames, text: new TextDecoder().decode(new Uint8Array(text)) };
  }
}

// An RPC result's strings: [command, length, len1, s1..., len2, s2...].
export function resultStrings(data) {
  const out = [];
  const dec = new TextDecoder();
  for (let i = 2; i < data.length;) {
    const n = data[i];
    out.push(dec.decode(new Uint8Array(data.slice(i + 1, i + 1 + n))));
    i += 1 + n;
  }
  return out;
}

// "0.13.10" vs "0.13.2": -1, 0 or 1.
export function compareVersions(a, b) {
  const pa = String(a).split(/[.-]/).map(Number), pb = String(b).split(/[.-]/).map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}
