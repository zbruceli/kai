"""Add TLS-PSK to links2004/WebSockets (pinned at 2.7.3), run before each build.

The library builds its WiFiClientSecure inside loop() and calls setInsecure() when no CA is given, which
also clears any pre-shared key, so there's no way to use PSK from outside. This adds
WebSocketsClient::beginSslWithPsk(host, port, url, identity, hex_key), which calls setPreSharedKey()
instead. Idempotent; fails the build if the library no longer looks as expected (e.g. after a version bump).
"""

from pathlib import Path

Import("env")  # noqa: F821  (provided by PlatformIO)

MARK = "kai: TLS-PSK"

PATCHES = {
    "WebSocketsClient.h": [
        (
            "    void beginSslWithClientKey(const char * host, uint16_t port, const char * url, const char * CA_cert, "
            "const char * clientCert, const char * clientPrivateKey, const char * protocol = \"arduino\");\n",
            "    void beginSslWithClientKey(const char * host, uint16_t port, const char * url, const char * CA_cert, "
            "const char * clientCert, const char * clientPrivateKey, const char * protocol = \"arduino\");\n"
            "#ifdef ESP32\n"
            f"    // {MARK}: pre-shared key instead of certificates; hex_key is 2..64 hex digits\n"
            "    void beginSslWithPsk(const char * host, uint16_t port, const char * url, const char * identity, "
            "const char * hex_key, const char * protocol = \"arduino\");\n"
            "#endif\n",
        ),
        (
            "    const char * _client_cert;\n    const char * _client_key;\n",
            "    const char * _client_cert;\n    const char * _client_key;\n"
            f"    const char * _psk_ident = NULL;  // {MARK}\n    const char * _psk_hex = NULL;\n",
        ),
    ],
    "WebSocketsClient.cpp": [
        (
            "void WebSocketsClient::beginSslWithClientKey(",
            "#if defined(ESP32)\n"
            f"// {MARK}\n"
            "void WebSocketsClient::beginSslWithPsk(const char * host, uint16_t port, const char * url, "
            "const char * identity, const char * hex_key, const char * protocol) {\n"
            "    beginSslWithCA(host, port, url, NULL, protocol);\n"
            "    _psk_ident = identity;\n"
            "    _psk_hex   = hex_key;\n"
            "}\n"
            "#endif\n\n"
            "void WebSocketsClient::beginSslWithClientKey(",
        ),
        (
            "            } else if(!SSL_FINGERPRINT_IS_SET) {\n                _client.ssl->setInsecure();\n",
            f"            }} else if(_psk_ident && _psk_hex) {{  // {MARK}\n"
            "                _client.ssl->setPreSharedKey(_psk_ident, _psk_hex);\n"
            "            } else if(!SSL_FINGERPRINT_IS_SET) {\n                _client.ssl->setInsecure();\n",
        ),
    ],
}


def patch() -> None:
    src = Path(env.subst("$PROJECT_LIBDEPS_DIR")) / env.subst("$PIOENV") / "WebSockets" / "src"  # noqa: F821
    if not src.is_dir():
        raise SystemExit(f"patch_websockets: {src} not found (lib_deps not installed yet?)")
    for name, edits in PATCHES.items():
        path = src / name
        text = path.read_text()
        if MARK in text:
            continue
        for old, new in edits:
            if text.count(old) != 1:
                raise SystemExit(f"patch_websockets: {name} doesn't match the pinned WebSockets 2.7.3")
            text = text.replace(old, new)
        path.write_text(text)
        print(f"patch_websockets: added TLS-PSK to {name}")


patch()
