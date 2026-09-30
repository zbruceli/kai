"""The encrypted Stick <-> relay link: TLS 1.2 with ECDHE-PSK.

Each device has its own 32-byte pre-shared key. Both sides prove they hold it during the handshake, so:
  - nobody on the Wi-Fi can read the audio or transcripts (encrypted),
  - nobody can pose as the relay or as a Stick (mutual authentication), and the device token never
    travels in the clear,
  - an ephemeral X25519 exchange gives forward secrecy: a key leaked later (a lost Stick) doesn't decrypt
    recordings of past traffic.
No certificates, so the Stick needs no clock. Measured on the Stick: ~0.2 s per handshake at 240 MHz
(docs/private/ENCRYPTION_COST.md). The ESP32-S3's mbedTLS has TLS 1.2 only, hence 1.2.

Python's PSK callbacks need Python 3.13 or newer.
"""

import argparse
import logging
import re
import secrets
import ssl

log = logging.getLogger("kai.tls")

PSK_BYTES = 32
# Forward-secret PSK suites only. mbedTLS on the Stick offers the AES one (AES and SHA-256 are in hardware);
# ChaCha20 is there for software clients such as kai-sim.
CIPHERS = "ECDHE-PSK-CHACHA20-POLY1305:ECDHE-PSK-AES128-CBC-SHA256"
IDENTITY = re.compile(r"^[\w-]{1,32}$")


def supported() -> bool:
    return hasattr(ssl.SSLContext, "set_psk_server_callback")


def parse_psks(value: str | None) -> dict[str, bytes]:
    """KAI_DEVICE_PSKS: "identity:hex,identity:hex" -> {identity: key}. Refuses weak or malformed keys."""
    keys: dict[str, bytes] = {}
    for entry in filter(None, (e.strip() for e in (value or "").split(","))):
        identity, _, hexkey = entry.partition(":")
        if not IDENTITY.match(identity):
            raise ValueError(f"bad PSK identity {identity!r}: letters, digits, '-' and '_', up to 32")
        try:
            key = bytes.fromhex(hexkey)
        except ValueError:
            raise ValueError(f"PSK for {identity} isn't hex") from None
        if len(key) != PSK_BYTES:
            raise ValueError(f"PSK for {identity} must be {PSK_BYTES} bytes ({2 * PSK_BYTES} hex characters)")
        keys[identity] = key
    return keys


def _base(side: int) -> ssl.SSLContext:
    ctx = ssl.SSLContext(side)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(CIPHERS)
    ctx.set_ecdh_curve("X25519")
    ctx.options |= ssl.OP_NO_COMPRESSION | ssl.OP_NO_RENEGOTIATION
    return ctx


def server_context(psks: dict[str, bytes]) -> ssl.SSLContext:
    if not supported():
        raise SystemExit("KAI_DEVICE_PSKS needs Python 3.13 or newer (ssl PSK callbacks)")
    ctx = _base(ssl.PROTOCOL_TLS_SERVER)

    def key_for(identity: str | None) -> bytes:
        key = psks.get(identity or "")
        if key is None:
            # An empty key fails the handshake: unknown devices never reach the WebSocket.
            log.warning("TLS: unknown device identity %r", (identity or "")[:40])
            return b""
        return key

    ctx.set_psk_server_callback(key_for)
    return ctx


def client_context(identity: str, key: bytes) -> ssl.SSLContext:
    """For kai-sim and tests: the Stick's side of the link."""
    if not supported():
        raise SystemExit("the encrypted link needs Python 3.13 or newer (ssl PSK callbacks)")
    ctx = _base(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # no certificates: the PSK authenticates the relay
    ctx.set_psk_client_callback(lambda hint: (identity, key))
    return ctx


def main() -> None:
    """kai-psk <identity>: make a new device key. Prints the relay .env entry and the firmware lines."""
    p = argparse.ArgumentParser(description=main.__doc__)
    p.add_argument("identity", help="the device's name, e.g. kai-4514 (letters, digits, - and _)")
    identity = p.parse_args().identity
    if not IDENTITY.match(identity):
        raise SystemExit("identity: letters, digits, '-' and '_', up to 32 characters")
    key = secrets.token_hex(PSK_BYTES)
    print(f"# relay/.env: add to KAI_DEVICE_PSKS (comma-separated for several devices)\n"
          f"KAI_DEVICE_PSKS={identity}:{key}\n\n"
          f"// firmware/src/secrets.h\n"
          f'#define KAI_PSK_IDENTITY "{identity}"\n#define KAI_PSK_HEX "{key}"')
