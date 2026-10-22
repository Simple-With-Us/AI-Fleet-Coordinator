"""Probe the MiniMax app-server socket:  it speaks JSON-RPC over WebSocket.

The app-server is the only externally reachable MiniMax surface.  `mcode app-server proxy`
is documented as "Proxy raw WebSocket bytes between stdin/stdout and a Unix socket", and the
runtime's own lifecycle client shows the protocol:  JSON-RPC over WebSocket at
ws://mcode.local/, with `initialize` carrying the transport protocol version, and the
reply's `capabilities.methods` naming every method the server offers.

This connects, initializes, and prints the server's own method catalogue.  It sends
nothing but the initialize and its notification, so it changes no state.
"""

import base64
import json
import os
import socket
import struct
import sys

SOCK = os.path.expanduser("~/.minimax/app-server-control/app-server.sock")
HOST = "mcode.local"
PROTOCOL_VERSION = 1
TRANSPORT_PROTOCOL_VERSION = 2


def upgrade(sock_path, path="/", host=HOST):
    """The HTTP/1.1 upgrade handshake, straight down the Unix socket."""
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n" % (path, host, key)
    )
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(15)
    s.connect(sock_path)
    s.sendall(request.encode())
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = s.recv(4096)
        if not chunk:
            break
        buffer += chunk
    head, _, rest = buffer.partition(b"\r\n\r\n")
    return s, head.decode("utf-8", "replace"), rest


def send_text(s, text):
    """One masked WebSocket text frame, as a client must send."""
    payload = text.encode("utf-8")
    header = bytearray([0x81])
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask
    header += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    s.sendall(bytes(header))


def read_frame(s, timeout=15):
    """One WebSocket frame, unmasked from the server."""
    s.settimeout(timeout)
    try:
        head = s.recv(2)
    except socket.timeout:
        return None, b""
    if not head:
        return None, b""
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", s.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", s.recv(8))[0]
    body = b""
    while len(body) < length:
        chunk = s.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    return head[0] & 0x0F, body


def main():
    if not os.path.exists(SOCK):
        print("no socket at %s" % SOCK)
        return 1
    try:
        s, head, rest = upgrade(SOCK)
    except Exception as exc:
        print("the upgrade failed: %s: %s" % (type(exc).__name__, exc))
        return 1
    print("--- upgrade ---")
    print(head.split("\r\n")[0])
    if "101" not in head.split("\r\n")[0]:
        print(head[:600])
        return 2
    send_text(s, json.dumps({
        "id": "lifecycle-1",
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "clientInfo": {"name": "mcode-probe"},
            "capabilities": {"desktopService": {"transportProtocolVersion": TRANSPORT_PROTOCOL_VERSION}},
        },
    }))
    opcode, body = read_frame(s)
    print("--- initialize reply (opcode %s) ---" % opcode)
    text = body.decode("utf-8", "replace")
    print(text[:1500])
    try:
        reply = json.loads(text)
    except Exception:
        return 3
    result = reply.get("result") or {}
    methods = (result.get("capabilities") or {}).get("methods") or []
    print("--- the server's method catalogue (%d) ---" % len(methods))
    for name in methods:
        print("   ", name)
    send_text(s, json.dumps({"method": "initialized"}))
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
