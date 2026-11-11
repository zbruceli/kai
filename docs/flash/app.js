// The flasher page: one connection to the Stick over Web Serial, used for everything.
// - status and settings: `kai-config {json}` lines (firmware/src/provision.cpp);
// - Wi-Fi: Improv serial frames (improv.js);
// - flashing: esptool-js, which takes the port over for the duration, then hands it back.
import { CMD, ERROR, Splitter, TYPE, compareVersions, resultStrings, rpc, wifiSettings } from "./improv.js";

const ESPTOOL = "https://cdn.jsdelivr.net/npm/esptool-js@0.6.1/bundle.js";  // Apache-2.0, pinned
const MANIFEST = "firmware/manifest.json";

const $ = id => document.getElementById(id);
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---------- the connection ----------

let port = null;
let reader = null, loopDone = null;
let splitter = new Splitter();
let lineBuf = "";
const frameWaiters = new Set();   // (frame) => boolean (true: handled, remove)
const lineWaiters = new Set();    // (line) => boolean
let flashing = false;

async function openPort() {
  await port.open({ baudRate: 115200, bufferSize: 8192 });
  splitter = new Splitter();
  lineBuf = "";
  loopDone = readLoop();
}

async function closePort() {
  if (!port) return;
  try { if (reader) await reader.cancel(); } catch {}
  try { await loopDone; } catch {}
  try { await port.close(); } catch {}
}

async function readLoop() {
  while (port && port.readable) {
    reader = port.readable.getReader();
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) return;
        const { frames, text } = splitter.push(value);
        for (const f of frames) for (const w of [...frameWaiters]) if (w(f)) frameWaiters.delete(w);
        if (text) onText(text);
      }
    } catch (e) {
      if (!flashing) log(`\n[connection lost: ${e.message}]\n`);
      return;
    } finally {
      try { reader.releaseLock(); } catch {}
      reader = null;
    }
  }
}

let lastLogNewline = true;
function onText(text) {
  // Each Improv frame ends with a newline of its own: don't let those pile up as empty lines.
  const shown = text.replace(/\n{2,}/g, "\n");
  log(lastLogNewline ? shown.replace(/^\n+/, "") : shown);
  if (shown) lastLogNewline = shown.endsWith("\n");
  lineBuf += text;
  const lines = lineBuf.split(/\r?\n|\r/);
  lineBuf = lines.pop().slice(-2048);
  for (const line of lines) for (const w of [...lineWaiters]) if (w(line)) lineWaiters.delete(w);
}

async function write(bytes) {
  const w = port.writable.getWriter();
  try { await w.write(bytes); } finally { w.releaseLock(); }
}

// Ask the Stick something over `kai-config` lines; null if it doesn't answer.
async function config(obj, tries = 3, waitMs = 1500) {
  for (let i = 0; i < tries; i++) {
    let handler;
    const reply = new Promise(resolve => {
      handler = line => {
        const at = line.indexOf("kai-config {");
        if (at < 0) return false;
        try { resolve(JSON.parse(line.slice(at + 11))); return true; } catch { return false; }
      };
      lineWaiters.add(handler);
      setTimeout(() => resolve(null), waitMs);
    });
    await write(new TextEncoder().encode(`\nkai-config ${JSON.stringify(obj)}\n`));
    const r = await reply;
    lineWaiters.delete(handler);
    if (r) return r;
  }
  return null;
}

// An Improv request: resolves with what `collect` gathers, or rejects on an error frame or timeout.
function improv(frame, collect, timeoutMs) {
  return new Promise((resolve, reject) => {
    let waiter;
    const timer = setTimeout(() => { frameWaiters.delete(waiter); reject(new Error("the Stick didn't answer")); }, timeoutMs);
    waiter = f => {
      if (f.type === TYPE.ERROR && f.data[0]) {
        clearTimeout(timer);
        reject(new Error(ERROR[f.data[0]] || "error"));
        return true;
      }
      const done = collect(f);
      if (done === undefined) return false;
      clearTimeout(timer);
      resolve(done);
      return true;
    };
    frameWaiters.add(waiter);
    write(frame).catch(reject);
  });
}

function scanNetworks() {
  const found = [];
  return improv(rpc(CMD.GET_NETWORKS), f => {
    if (f.type !== TYPE.RESULT || f.data[0] !== CMD.GET_NETWORKS) return undefined;
    const [ssid, rssi, secured] = resultStrings(f.data);
    if (ssid === undefined) return found;  // an empty result ends the list
    found.push({ ssid, rssi: Number(rssi), secured: secured === "YES" });
    return undefined;
  }, 15000);
}

// Joins the network first, and only then saves it; resolves once joined.
function joinWifi(ssid, password) {
  return improv(wifiSettings(ssid, password),
    f => (f.type === TYPE.RESULT && f.data[0] === CMD.WIFI_SETTINGS ? true : undefined), 30000);
}

async function restartStick() {
  // The ESP32-S3's own USB port: RTS pulls the chip's enable line, as esptool does.
  await port.setSignals({ dataTerminalReady: false, requestToSend: true });
  await sleep(100);
  await port.setSignals({ dataTerminalReady: false, requestToSend: false });
}

// ---------- flashing ----------

async function fetchParts(manifest, onProgress) {
  const build = manifest.builds.find(b => b.chipFamily === "ESP32-S3");
  const base = new URL(MANIFEST, location.href);
  const parts = [];
  for (const part of build.parts) {
    const resp = await fetch(new URL(part.path, base));
    if (!resp.ok) throw new Error(`couldn't download ${part.path} (${resp.status})`);
    parts.push({ data: new Uint8Array(await resp.arrayBuffer()), address: part.offset });
    onProgress?.();
  }
  return parts;
}

async function flash({ erase }) {
  const total = () => fileArray.reduce((n, f) => n + f.data.length, 0);
  let fileArray = [];
  flashing = true;
  showPanel("progress");
  setProgress("Downloading the firmware…", 0);
  try {
    fileArray = await fetchParts(manifest);
    const { ESPLoader, Transport } = await import(ESPTOOL);
    await closePort();
    setProgress("Connecting to the Stick's bootloader…", 0);
    const transport = new Transport(port, false);
    const terminal = { clean() {}, writeLine: s => log(`${s}\n`), write: s => log(s) };
    const loader = new ESPLoader({ transport, baudrate: 115200, terminal, enableTracing: false });
    try {
      await loader.main();
      if (!/ESP32-S3/.test(loader.chip.CHIP_NAME)) throw new Error(`this is an ${loader.chip.CHIP_NAME}, not a Stick (ESP32-S3)`);
      if (erase) {
        setProgress("Erasing the Stick (about 30 seconds)…", 0);
        await loader.eraseFlash();
      }
      let done = 0;
      const size = total();
      await loader.writeFlash({
        fileArray, flashSize: "keep", flashMode: "keep", flashFreq: "keep", eraseAll: false, compress: true,
        reportProgress: (i, written, of) => {
          const part = (written / of) * fileArray[i].data.length;
          setProgress(`Installing Kai ${manifest.version}…`, Math.floor(((done + part) / size) * 100));
          if (written === of) done += fileArray[i].data.length;
        },
      });
      setProgress("Installed. Restarting the Stick…", 100);
      await transport.setRTS(true);
      await sleep(100);
      await loader.after();
    } finally {
      try { await transport.disconnect(); } catch {}
    }
    await sleep(200);
    await openPort();
    flashing = false;
    await refresh({ waitForBoot: true });
    if (erase || !state?.wifi) {
      note(`Kai ${manifest.version} is installed. Now connect it to Wi-Fi.`, "ok");
      openWifi();
    } else {
      note(`Updated to Kai ${manifest.version}. Wi-Fi and pairing are kept.`, "ok");
      showPanel(null);
    }
  } catch (e) {
    flashing = false;
    note(`Install failed: ${e.message}. Unplug the Stick, plug it back in, and try again; if it still fails, ` +
         `hold the side button for 2 seconds (download mode) first.`, "bad");
    showPanel(null);
    try { if (!port.readable) await openPort(); } catch {}
  }
}

// ---------- the page ----------

let manifest = null;
let state = null;  // the Stick's last kai-config reply, or null when it isn't running Kai

function log(text) {
  const el = $("log");
  el.textContent = (el.textContent + text).slice(-60000);
  if ($("follow").checked) el.scrollTop = el.scrollHeight;
}

function note(text, cls = "soft") {
  const el = $("note");
  el.textContent = text;
  el.className = `note ${cls}`;
  el.hidden = !text;
}

function setProgress(text, pct) {
  $("progress-text").textContent = text;
  $("progress-bar").style.width = `${pct}%`;
  $("progress-bar").parentElement.setAttribute("aria-valuenow", String(pct));
}

function showPanel(name) {
  for (const p of ["progress", "wifi", "logs"]) $(`p-${p}`).hidden = p !== name;
  $("actions").hidden = name === "progress";
  for (const b of document.querySelectorAll(".action")) b.setAttribute("aria-expanded", String(b.dataset.panel === name));
}

function row(label, value) {
  const dt = document.createElement("dt"); dt.textContent = label;
  const dd = document.createElement("dd"); dd.textContent = value;
  return [dt, dd];
}

function render() {
  const s = state;
  const v = manifest?.version ?? "";
  $("d-name").textContent = s ? s.device : "A Stick that isn't running Kai yet";
  $("d-fw").textContent = s ? `Kai ${s.fw}` : "Blank, other firmware, or still starting up";
  const dl = $("status");
  dl.replaceChildren();
  if (s) {
    dl.append(
      ...row("Wi-Fi", s.wifi ? `${s.wifi} · ${s.wifi_connected ? "connected" : "not connected"}` : "not set up"),
      ...row("Relay", s.relay ? `${s.relay} · set by hand` : s.found ? `${s.found} · found on the network` : "not found yet"),
      ...row("Paired", s.key ? `yes, as ${s.key}` : s.pairing && s.pairing !== "idle" ? s.pairing : "not yet"),
    );
  }
  // 1. Update (keeps settings). Only for a Stick already running Kai.
  const cmp = s ? compareVersions(v, s.fw) : 0;
  $("a-update").hidden = !s;
  $("a-update-title").textContent = cmp > 0 ? `Update to Kai ${v}` : `Reinstall Kai ${v}`;
  $("a-update-desc").textContent = cmp > 0
    ? `This Stick runs ${s?.fw}. Keeps Wi-Fi and pairing.`
    : cmp === 0 ? "Already up to date. Keeps Wi-Fi and pairing." : `This Stick runs a newer ${s?.fw}. Keeps Wi-Fi and pairing.`;
  $("a-update").classList.toggle("primary", cmp > 0);
  // 2. Fresh install (erases). The main action for a blank Stick.
  $("a-fresh-title").textContent = s ? "Fresh install" : `Install Kai ${v}`;
  $("a-fresh-desc").textContent = s
    ? "Erases the Stick, Wi-Fi and pairing included, then installs Kai. It pairs again afterwards."
    : "Erases the Stick and installs Kai. Then you'll set up Wi-Fi.";
  $("a-fresh").classList.toggle("primary", !s);
  $("a-fresh").classList.toggle("danger", !!s);  // erasing only costs something once there's something to lose
  // 3. Wi-Fi, 4. Logs.
  $("a-wifi").hidden = !s;
  $("a-wifi-desc").textContent = s?.wifi ? `On ${s.wifi}${s.wifi_connected ? "" : " (not connected)"}. Change network.` : "Not set up yet.";
  $("more").hidden = !s;
  $("relay").value = s?.relay || "";
}

async function refresh({ waitForBoot = false } = {}) {
  // Right after a restart the Stick needs a few seconds before it answers.
  state = await config({}, waitForBoot ? 8 : 3, 1500);
  render();
  return state;
}

async function connect() {
  if (!("serial" in navigator)) return;
  try {
    port = await navigator.serial.requestPort({ filters: [{ usbVendorId: 0x303a }] });  // Espressif USB
    await openPort();
  } catch (e) {
    port = null;
    if (e.name === "NotFoundError") return;  // closed the picker
    note(e.name === "InvalidStateError" || /Failed to open/.test(e.message)
      ? "The Stick's port is busy: close other tabs or apps using it (another flasher tab, a serial monitor), then try again."
      : `Couldn't open the port: ${e.message}`, "bad");
    return;
  }
  note("");
  $("v-disconnected").hidden = true;
  $("v-connected").hidden = false;
  $("d-name").textContent = "Asking the Stick…";
  $("d-fw").textContent = "";
  showPanel(null);
  await refresh();
}

async function disconnect() {
  await closePort();
  port = null;
  state = null;
  $("v-connected").hidden = true;
  $("v-disconnected").hidden = false;
}

function openWifi() {
  showPanel("wifi");
  $("wifi-pass").value = "";
  $("wifi-msg").textContent = "";
  scan();
}

async function scan() {
  const sel = $("wifi-ssid");
  sel.replaceChildren(new Option("Looking for networks…", ""));
  sel.disabled = true;
  try {
    const nets = await scanNetworks();
    sel.replaceChildren(...nets.map(n => new Option(`${n.ssid}${n.secured ? "" : " (open)"}`, n.ssid)),
                        new Option("Other network…", "__other"));
    if (state?.wifi && nets.some(n => n.ssid === state.wifi)) sel.value = state.wifi;
  } catch (e) {
    sel.replaceChildren(new Option("Other network…", "__other"));
    $("wifi-msg").textContent = `Couldn't list networks (${e.message}); type the name instead.`;
  }
  sel.disabled = false;
  $("wifi-other").hidden = sel.value !== "__other";
}

async function saveWifi(ev) {
  ev.preventDefault();
  const ssid = $("wifi-ssid").value === "__other" ? $("wifi-other").value.trim() : $("wifi-ssid").value;
  if (!ssid) return;
  const btn = $("wifi-save");
  btn.disabled = true;
  $("wifi-msg").className = "soft";
  $("wifi-msg").textContent = `Joining ${ssid}…`;
  try {
    await joinWifi(ssid, $("wifi-pass").value);
    $("wifi-pass").value = "";
    await refresh();
    showPanel(null);
    note(state?.key ? `On ${ssid}.` : `On ${ssid}. The Stick now looks for the relay and shows a code: see step 2 below.`, "ok");
  } catch (e) {
    $("wifi-msg").className = "bad";
    $("wifi-msg").textContent = `${e.message[0].toUpperCase()}${e.message.slice(1)}. The Stick kept its previous Wi-Fi.`;
  } finally {
    btn.disabled = false;
  }
}

async function settings(obj, done) {
  const r = await config(obj);
  if (!r) return note("The Stick didn't answer.", "bad");
  state = r;
  render();
  note(r.ok === false ? `The Stick said no: ${r.error}` : done, r.ok === false ? "bad" : "ok");
}

// ---------- wiring ----------

fetch(MANIFEST).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }).then(m => {
  manifest = m;
  $("fw-latest").textContent = `Latest firmware: Kai ${m.version}`;
}).catch(() => {
  $("no-firmware").hidden = false;
  $("connect").disabled = true;
});

if (!("serial" in navigator)) {
  $("connect").disabled = true;
  $("unsupported").hidden = false;
} else if (!window.isSecureContext) {
  $("connect").disabled = true;
  $("unsupported").hidden = false;
}

$("connect").addEventListener("click", connect);
$("disconnect").addEventListener("click", () => { disconnect(); note(""); });
$("a-update").addEventListener("click", () => flash({ erase: false }));
$("a-fresh").addEventListener("click", () => {
  const msg = state
    ? `Erase ${state.device} and install Kai ${manifest.version}? Its Wi-Fi and pairing are deleted; it pairs again afterwards.`
    : `Erase this Stick and install Kai ${manifest.version}?`;
  if (confirm(msg)) flash({ erase: true });
});
$("a-wifi").addEventListener("click", () => ($("p-wifi").hidden ? openWifi() : showPanel(null)));
$("a-logs").addEventListener("click", () => showPanel($("p-logs").hidden ? "logs" : null));
$("p-wifi").addEventListener("submit", saveWifi);
$("wifi-cancel").addEventListener("click", () => showPanel(null));
$("wifi-ssid").addEventListener("change", () => { $("wifi-other").hidden = $("wifi-ssid").value !== "__other"; });
$("logs-close").addEventListener("click", () => showPanel(null));
$("restart").addEventListener("click", async () => { await restartStick(); log("\n[restarting the Stick]\n"); });
$("console-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const line = $("console-input").value;
  $("console-input").value = "";
  await write(new TextEncoder().encode(`${line}\n`));
});
$("save-relay").addEventListener("click", () => {
  const relay = $("relay").value.trim();
  settings({ relay }, relay ? `Saved: the Stick now uses ${relay}.` : "Cleared: the Stick finds the relay on the network.");
});
$("unpair").addEventListener("click", () => {
  if (confirm("Forget this Stick's key? It will show a new code to approve on the relay.")) {
    settings({ unpair: true }, "Key forgotten: approve the new code the Stick shows (step 2).");
  }
});
navigator.serial?.addEventListener("disconnect", ev => {
  if (ev.target === port && !flashing) { disconnect(); note("The Stick was unplugged."); }
});
