"""Pairing a new Stick with a code (kai_relay/pairing.py)."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from kai_relay import pairing, tls

# The shared test vector: firmware/test/test_pairing/test_main.cpp asserts the same bytes.
VEC = {
    "stick_priv": bytes(range(1, 33)), "relay_priv": bytes(range(33, 65)),
    "ns": bytes(0xA0 + i for i in range(16)), "nr": bytes(0xB0 + i for i in range(16)), "id": "kai-test",
    "ps": "07a37cbc142093c8b755dc1b10e86cb426374ad16aa853ed0bdfc0b2b86d1c7c",
    "pr": "5869aff450549732cbaaed5e5df9b30a6da31cb0e5742bad5ad4a1a768f1a67b",
    "commit": "10092636b07f9cf39043b62d05badd8a26ad895a05e99b5c8679ba8d275546f6"
              "7578693ea5cfdc8f4a131e519e5ebc4c0e16c27b403df0a2c5e3f9b3b80bf8e8",
    "key": "e4fa513e8a9f8c3743b04239eeafcd839e0707792e789b171a5a01dcdaf628a4", "code": "215831",
}


def test_vector():
    sp = X25519PrivateKey.from_private_bytes(VEC["stick_priv"])
    rp = X25519PrivateKey.from_private_bytes(VEC["relay_priv"])
    ps, pr = pairing.public_bytes(sp), pairing.public_bytes(rp)
    assert ps.hex() == VEC["ps"] and pr.hex() == VEC["pr"]
    assert pairing.commitment(pr, ps, VEC["nr"]).hex() == VEC["commit"]
    for own, theirs in ((sp, pr), (rp, ps)):  # both sides get the same key and code
        key, code = pairing.derive(own, theirs, VEC["id"], ps, pr, VEC["ns"], VEC["nr"])
        assert (key.hex(), code) == (VEC["key"], VEC["code"])
    other, _ = pairing.derive(sp, pr, "kai-other", ps, pr, VEC["ns"], VEC["nr"])
    assert other.hex() != VEC["key"]  # the key is bound to the device's name
    with pytest.raises(ValueError):
        pairing.derive(sp, bytes(32), VEC["id"], ps, bytes(32), VEC["ns"], VEC["nr"])  # low-order point


async def stick_side(url: str, identity: str):
    """What the firmware does: hello, check the commitment, derive. Returns (key, code)."""
    own = X25519PrivateKey.generate()
    ps, ns = pairing.public_bytes(own), bytes(range(16))
    async with connect(url) as ws:
        await ws.send(json.dumps({"type": "pair_hello", "v": 1, "device": identity, "pub": ps.hex()}))
        c = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "pair_nonce", "nonce": ns.hex()}))
        n = json.loads(await ws.recv())
    pr, nr = bytes.fromhex(c["pub"]), bytes.fromhex(n["nonce"])
    assert pairing.commitment(pr, ps, nr).hex() == c["commit"], "relay changed its nonce after committing"
    return pairing.derive(own, pr, identity, ps, pr, ns, nr)


def store_at(tmp_path):
    return pairing.DeviceStore(tmp_path / "devices.db")


def test_pair_approve_then_encrypted_link(tmp_path):
    """The whole flow: pair, a wrong code is refused, the right one approves, and only then does the
    encrypted link accept the Stick's new key."""
    store = store_at(tmp_path)
    service = pairing.PairingService(store, min_gap_s=0)

    async def echo(ws):
        await ws.send(await ws.recv())

    async def go():
        async with serve(service.handle, "127.0.0.1", 0) as ps, \
                serve(echo, "127.0.0.1", 0, ssl=tls.server_context(store)) as ts:
            pair_url = f"ws://127.0.0.1:{ps.sockets[0].getsockname()[1]}/pair"
            tls_url = f"wss://127.0.0.1:{ts.sockets[0].getsockname()[1]}/ws"
            key, code = await stick_side(pair_url, "kai-7f3a")
            assert [p["code"] for p in store.pending()] == [code]

            async def link_works():
                try:
                    async with connect(tls_url, ssl=tls.client_context("kai-7f3a", key), open_timeout=5) as ws:
                        await ws.send("hi")
                        return await ws.recv() == "hi"
                except Exception:
                    return False

            assert not await link_works()                          # not approved yet
            wrong = f"{(int(code) + 1) % 1_000_000:06d}"
            assert store.approve(wrong) is None
            assert store.approve(f"{code[:3]} {code[3:]}") == "kai-7f3a"  # spaces as shown on screen are fine
            assert store.pending() == [] and await link_works()
            assert store.revoke("kai-7f3a") and not await link_works()   # revoked: refused at once

    asyncio.run(go())


def test_man_in_the_middle_gets_mismatched_codes(tmp_path):
    """An attacker between the Stick and the relay runs its own keys towards each. The two codes differ, so
    kai-approve with the code on the Stick finds nothing to approve."""
    store = store_at(tmp_path)
    service = pairing.PairingService(store, min_gap_s=0)

    async def go():
        async with serve(service.handle, "127.0.0.1", 0) as real:
            relay_url = f"ws://127.0.0.1:{real.sockets[0].getsockname()[1]}/pair"
            relay_codes = []

            async def attacker(stick_ws):
                # Towards the Stick it pretends to be the relay (with its own key and nonce)...
                hello = json.loads(await stick_ws.recv())
                fake = X25519PrivateKey.generate()
                fpub, fnonce = pairing.public_bytes(fake), bytes(range(100, 116))
                ps = bytes.fromhex(hello["pub"])
                await stick_ws.send(json.dumps({"type": "pair_commit", "pub": fpub.hex(),
                                                "commit": pairing.commitment(fpub, ps, fnonce).hex()}))
                json.loads(await stick_ws.recv())
                await stick_ws.send(json.dumps({"type": "pair_nonce", "nonce": fnonce.hex()}))
                # ...and towards the relay it pretends to be the Stick, under the same name.
                relay_codes.append((await stick_side(relay_url, hello["device"]))[1])

            async with serve(attacker, "127.0.0.1", 0) as evil:
                _, stick_code = await stick_side(f"ws://127.0.0.1:{evil.sockets[0].getsockname()[1]}/pair", "kai-7f3a")
            await asyncio.sleep(0.2)
            return stick_code, relay_codes[0]

    stick_code, relay_code = asyncio.run(go())
    assert stick_code != relay_code                 # the owner sees two different codes
    assert store.approve(stick_code) is None        # and approving what the Stick shows does nothing


def test_limits_expiry_window_and_env_import(tmp_path):
    store = store_at(tmp_path)

    async def pair_n(n, gap):
        service = pairing.PairingService(store, min_gap_s=gap)
        async with serve(service.handle, "127.0.0.1", 0) as s:
            url = f"ws://127.0.0.1:{s.sockets[0].getsockname()[1]}/pair"
            results = []
            for i in range(n):
                try:
                    results.append((await stick_side(url, f"kai-{i:04d}"))[1])
                except Exception:
                    results.append(None)
            return results

    # One request per address per gap.
    assert asyncio.run(pair_n(2, 60))[1] is None
    # At most MAX_PENDING waiting at once.
    store.db.execute("DELETE FROM pending")
    results = asyncio.run(pair_n(pairing.MAX_PENDING + 1, 0))
    assert all(results[:pairing.MAX_PENDING]) and results[-1] is None
    # Requests expire.
    store.db.execute("UPDATE pending SET expires_at = ?", ((datetime.now() - timedelta(seconds=1)).isoformat(),))
    store.db.commit()
    assert store.pending() == [] and store.approve(results[0]) is None
    # A pairing window approves the next Stick without typing its code, once.
    store.open_window(2)
    first, second = asyncio.run(pair_n(2, 0))
    assert store.get("kai-0000") is not None and store.get("kai-0001") is None
    # .env keys are imported once; a revoked one isn't brought back.
    key = bytes(range(32))
    store.import_env({"kai-env": key})
    assert store.get("kai-env") == key and store.revoke("kai-env")
    store.import_env({"kai-env": key})
    assert store.get("kai-env") is None


def test_bad_requests_are_refused(tmp_path):
    store = store_at(tmp_path)
    service = pairing.PairingService(store, min_gap_s=0)

    async def attempt(hello):
        async with serve(service.handle, "127.0.0.1", 0) as s:
            async with connect(f"ws://127.0.0.1:{s.sockets[0].getsockname()[1]}/pair") as ws:
                await ws.send(json.dumps(hello))
                try:
                    return json.loads(await ws.recv())
                except Exception:
                    return None

    good_pub = pairing.public_bytes(X25519PrivateKey.generate()).hex()
    for hello in ({"type": "pair_hello", "v": 1, "device": "bad name!", "pub": good_pub},
                  {"type": "pair_hello", "v": 2, "device": "kai-1", "pub": good_pub},
                  {"type": "pair_hello", "v": 1, "device": "kai-1", "pub": "abcd"},
                  {"type": "hello", "device": "kai-1"}):
        assert asyncio.run(attempt(hello)) is None, hello
    assert store.pending() == []


def test_revoked_device_connection_is_closed():
    from kai_relay import session

    closed = []

    class FakeWs:
        async def close(self, code, reason):
            closed.append((code, reason))

    class FakeSession:
        ws = FakeWs()

    session._ACTIVE["kai-gone"] = FakeSession()
    session._ACTIVE["kai-kept"] = FakeSession()
    try:
        asyncio.run(session.close_revoked(lambda name: name == "kai-gone"))
    finally:
        session._ACTIVE.pop("kai-gone", None)
        session._ACTIVE.pop("kai-kept", None)
    assert closed == [(4003, "revoked")]
