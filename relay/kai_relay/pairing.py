"""Pairing a new Stick with a code: the two sides agree a device key, and the owner confirms it.

The protocol follows Bluetooth LE Secure Connections' numeric comparison. Both sides make an X25519 key
pair. The relay commits to its nonce before it sees the Stick's, so a man in the middle gets one guess at
making the two 6-digit codes match (one in a million). The key itself never crosses the network.

    Stick -> relay   {"type": "pair_hello", "v": 1, "device": ID, "pub": PS}
    relay -> Stick   {"type": "pair_commit", "pub": PR, "commit": SHA512("kai-pair-v1 commit" PR PS NR)}
    Stick -> relay   {"type": "pair_nonce", "nonce": NS}
    relay -> Stick   {"type": "pair_nonce", "nonce": NR, "expires_s": 300}       (the Stick checks commit)
    both:  key  = HKDF-SHA512(X25519 secret, salt = NS NR, info = "kai-pair-v1 psk" ID PS PR)   32 bytes
           code = first 4 bytes of SHA512("kai-pair-v1 code" ID PS PR NS NR), mod 1,000,000

Then the Stick shows the code. The owner runs `kai-approve <code>` on the relay, which only succeeds if
the relay's code matches, and presses the Stick's front button, which makes the Stick save the key. The
Stick's side lives in firmware/src/pairing_crypto.cpp (Monocypher); test vectors keep both in step.

Device keys live in SQLite (data/devices.db), so `kai-approve` and `kai-revoke` change them while the
relay runs. Keys from KAI_DEVICE_PSKS in .env are imported once.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from .tls import IDENTITY, PSK_BYTES

log = logging.getLogger("kai.pairing")

LABEL = b"kai-pair-v1"
NONCE_BYTES = 16
PENDING_S = 300        # a request waits this long for kai-approve
MAX_PENDING = 3        # requests waiting at once
MIN_GAP_S = 10         # between requests from one address
STEP_TIMEOUT_S = 10    # for each message of the exchange


# ---- the crypto (mirrored in firmware/src/pairing_crypto.cpp) ----

def public_bytes(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def commitment(relay_pub: bytes, stick_pub: bytes, relay_nonce: bytes) -> bytes:
    return hashlib.sha512(LABEL + b" commit" + relay_pub + stick_pub + relay_nonce).digest()


def derive(own: X25519PrivateKey, their_pub: bytes, identity: str, stick_pub: bytes, relay_pub: bytes,
           stick_nonce: bytes, relay_nonce: bytes) -> tuple[bytes, str]:
    """(device key, 6-digit code). Raises ValueError for a bad public key (e.g. a low-order point)."""
    shared = own.exchange(X25519PublicKey.from_public_bytes(their_pub))
    ident = identity.encode()
    key = HKDF(hashes.SHA512(), PSK_BYTES, salt=stick_nonce + relay_nonce,
               info=LABEL + b" psk" + ident + stick_pub + relay_pub).derive(shared)
    digest = hashlib.sha512(LABEL + b" code" + ident + stick_pub + relay_pub + stick_nonce + relay_nonce).digest()
    return key, f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


# ---- device keys ----

# Times are stored in UTC: the relay and the commands (kai-approve over SSH) may run in different time zones.
def _utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now() -> str:
    return _utc().isoformat()


def _when(stored: str) -> datetime:
    t = datetime.fromisoformat(stored)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class DeviceStore:
    """Approved device keys and pending pairing requests. `get` is what the TLS handshake asks."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                identity TEXT PRIMARY KEY,
                key_hex TEXT NOT NULL,
                status TEXT NOT NULL,          -- approved | revoked
                source TEXT NOT NULL,          -- paired | env
                created_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY,
                identity TEXT NOT NULL,
                key_hex TEXT NOT NULL,
                code TEXT NOT NULL,
                peer TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        with self.db:  # rows from before times were stored in UTC (no offset): too ambiguous to keep
            self.db.execute("DELETE FROM pending WHERE expires_at NOT LIKE '%+00:00'")
            self.db.execute("DELETE FROM kv WHERE key = 'pair_window_until' AND value NOT LIKE '%+00:00'")

    # The TLS handshake: an approved device's key, or None.
    def get(self, identity: str | None) -> bytes | None:
        row = self.db.execute("SELECT key_hex FROM devices WHERE identity = ? AND status = 'approved'",
                              (identity or "",)).fetchone()
        return bytes.fromhex(row["key_hex"]) if row else None

    def status(self, identity: str) -> str | None:
        row = self.db.execute("SELECT status FROM devices WHERE identity = ?", (identity,)).fetchone()
        return row["status"] if row else None

    def approved_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM devices WHERE status = 'approved'").fetchone()[0]

    def import_env(self, psks: dict[str, bytes]) -> None:
        """KAI_DEVICE_PSKS from .env, once: a key later revoked or re-paired here is left alone."""
        for identity, key in psks.items():
            with self.db:
                cur = self.db.execute("INSERT OR IGNORE INTO devices (identity, key_hex, status, source, created_at) "
                                      "VALUES (?, ?, 'approved', 'env', ?)", (identity, key.hex(), _now()))
            if cur.rowcount:
                log.info("imported device key for %s from KAI_DEVICE_PSKS", identity)

    def _expire(self) -> None:
        now = _utc()
        expired = [r["id"] for r in self.db.execute("SELECT id, expires_at FROM pending") if _when(r["expires_at"]) < now]
        if expired:
            with self.db:
                self.db.executemany("DELETE FROM pending WHERE id = ?", [(i,) for i in expired])

    def pending(self) -> list[dict]:
        self._expire()
        return [dict(r) for r in self.db.execute("SELECT * FROM pending ORDER BY id")]

    def add_pending(self, identity: str, key: bytes, code: str, peer: str) -> bool:
        """Store a finished exchange. Returns True if a pairing window approved it straight away."""
        self._expire()
        expires = _utc() + timedelta(seconds=PENDING_S)
        with self.db:
            self.db.execute("DELETE FROM pending WHERE identity = ?", (identity,))  # newest request wins
            self.db.execute("INSERT INTO pending (identity, key_hex, code, peer, created_at, expires_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (identity, key.hex(), code, peer, _now(), expires.isoformat()))
        if self._window_open():
            with self.db:
                self.db.execute("DELETE FROM kv WHERE key = 'pair_window_until'")  # first device only
            return self.approve(code) == identity
        return False

    def approve(self, code: str) -> str | None:
        """Approve the pending request with this code. Returns its identity, or None if there's none."""
        self._expire()
        code = "".join(ch for ch in code if ch.isdigit())
        for row in self.db.execute("SELECT * FROM pending ORDER BY id DESC").fetchall():
            if secrets.compare_digest(row["code"], code):
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO devices (identity, key_hex, status, source, created_at) "
                                    "VALUES (?, ?, 'approved', 'paired', ?)", (row["identity"], row["key_hex"], _now()))
                    self.db.execute("DELETE FROM pending WHERE identity = ?", (row["identity"],))
                log.info("device %s approved", row["identity"])
                return row["identity"]
        return None

    def revoke(self, identity: str) -> bool:
        with self.db:
            cur = self.db.execute("UPDATE devices SET status = 'revoked', revoked_at = ? "
                                  "WHERE identity = ? AND status = 'approved'", (_now(), identity))
        return cur.rowcount > 0

    def devices(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT identity, status, source, created_at, revoked_at "
                                                 "FROM devices ORDER BY identity")]

    def open_window(self, minutes: float) -> datetime:
        until = _utc() + timedelta(minutes=minutes)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES ('pair_window_until', ?)", (until.isoformat(),))
        return until

    def _window_open(self) -> bool:
        row = self.db.execute("SELECT value FROM kv WHERE key = 'pair_window_until'").fetchone()
        return bool(row) and _when(row["value"]) > _utc()


# ---- the pairing endpoint (plain WebSocket: nothing secret crosses it) ----

class PairingService:
    def __init__(self, store: DeviceStore, min_gap_s: float = MIN_GAP_S):
        self.store = store
        self.min_gap_s = min_gap_s
        self._last_by_peer: dict[str, float] = {}

    async def handle(self, ws: ServerConnection) -> None:
        peer = ws.remote_address[0] if ws.remote_address else "?"
        if ws.request.path != "/pair":
            await ws.close(1008, "unknown path")
            return
        now = time.monotonic()
        if now - self._last_by_peer.get(peer, -1e9) < self.min_gap_s:
            await ws.close(1013, "slow down")
            return
        self._last_by_peer[peer] = now
        if len(self.store.pending()) >= MAX_PENDING:
            log.warning("pairing request from %s refused: %d already waiting", peer, MAX_PENDING)
            await ws.close(1013, "too many requests waiting")
            return
        try:
            await self._exchange(ws, peer)
        except (ValueError, KeyError, TypeError, TimeoutError, json.JSONDecodeError) as e:
            log.warning("pairing with %s failed: %s", peer, str(e)[:80])
            await ws.close(1008, "pairing failed")
        except ConnectionClosed:
            pass

    async def _recv(self, ws: ServerConnection, kind: str) -> dict:
        msg = json.loads(await asyncio.wait_for(ws.recv(), STEP_TIMEOUT_S))
        if not isinstance(msg, dict) or msg.get("type") != kind:
            raise ValueError(f"expected {kind}")
        return msg

    async def _exchange(self, ws: ServerConnection, peer: str) -> None:
        hello = await self._recv(ws, "pair_hello")
        identity = str(hello.get("device", ""))
        if hello.get("v") != 1 or not IDENTITY.match(identity):
            raise ValueError("bad hello")
        stick_pub = bytes.fromhex(str(hello["pub"]))
        if len(stick_pub) != 32:
            raise ValueError("bad public key")
        own = X25519PrivateKey.generate()
        relay_pub, relay_nonce = public_bytes(own), secrets.token_bytes(NONCE_BYTES)
        await ws.send(json.dumps({"type": "pair_commit", "pub": relay_pub.hex(),
                                  "commit": commitment(relay_pub, stick_pub, relay_nonce).hex()}))
        stick_nonce = bytes.fromhex(str((await self._recv(ws, "pair_nonce"))["nonce"]))
        if len(stick_nonce) != NONCE_BYTES:
            raise ValueError("bad nonce")
        key, code = derive(own, stick_pub, identity, stick_pub, relay_pub, stick_nonce, relay_nonce)
        await ws.send(json.dumps({"type": "pair_nonce", "nonce": relay_nonce.hex(), "expires_s": PENDING_S}))
        if self.store.add_pending(identity, key, code, peer):
            log.info("pairing window: %s approved automatically (code %s %s)", identity, code[:3], code[3:])
        else:
            log.info("pairing request from %s (%s): code %s %s; if the Stick shows the same code, run: "
                     "kai-approve %s", identity, peer, code[:3], code[3:], code)


# ---- commands ----

def _store() -> DeviceStore:
    from .config import load_settings
    return DeviceStore(load_settings().data_dir / "devices.db")


def devices_main() -> None:
    """kai-devices: list paired devices and pairing requests waiting for approval."""
    argparse.ArgumentParser(description=devices_main.__doc__).parse_args()
    store = _store()
    for d in store.devices():
        print(f"{d['identity']:12} {d['status']:8} ({d['source']}, since {d['created_at']})")
    for p in store.pending():
        left = (_when(p["expires_at"]) - _utc()).total_seconds() / 60
        print(f"{p['identity']:12} waiting  code {p['code'][:3]} {p['code'][3:]} from {p['peer']}, "
              f"{max(left, 0):.0f} min left -> kai-approve {p['code']}")
    if not store.devices() and not store.pending():
        print("no devices yet")


def approve_main() -> None:
    """kai-approve <code>: approve the Stick showing this code (only if the relay's code matches)."""
    p = argparse.ArgumentParser(description=approve_main.__doc__)
    p.add_argument("code", nargs="+", help="the 6 digits on the Stick's screen")
    identity = _store().approve("".join(p.parse_args().code))
    if not identity:
        raise SystemExit("No pairing request with that code. Check the digits; requests expire after "
                         f"{PENDING_S // 60} minutes. If the codes really differ, don't press the Stick's button: "
                         "something may be in between.")
    print(f"Approved {identity}. Now press the front button on the Stick.")


def revoke_main() -> None:
    """kai-revoke <name>: cut a Stick off. Its connection closes and it can't reconnect until paired again."""
    p = argparse.ArgumentParser(description=revoke_main.__doc__)
    p.add_argument("name")
    name = p.parse_args().name
    if not _store().revoke(name):
        raise SystemExit(f"No approved device called {name} (see kai-devices).")
    print(f"Revoked {name}.")


def pair_main() -> None:
    """kai-pair --open [minutes]: approve the next Stick that pairs, without typing its code.
    Anyone on your network pairing during the window gets in, so glance at the codes anyway."""
    p = argparse.ArgumentParser(description=pair_main.__doc__)
    p.add_argument("--open", type=float, metavar="MINUTES", nargs="?", const=2.0, required=True)
    minutes = min(max(p.parse_args().open, 0.5), 10.0)
    until = _store().open_window(minutes)
    print(f"Pairing window open until {until.astimezone():%H:%M:%S}: the next Stick to pair is approved "
          "automatically.")
