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
    send_frame(s, 0x1, text.encode("utf-8"))


MAX_FRAME = 1024 * 1024
INITIALIZE_TIMEOUT = 300  # the sibling MiniMax client (scripts/lark-mm-bridge.py) waits this long


class Reader:
    """Buffered frame reader:  keeps the bytes the upgrade read past its headers."""

    def __init__(self, sock, buffered=b""):
        self.sock = sock
        self.buf = bytearray(buffered)

    def _exactly(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("connection closed")
            self.buf += chunk
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def frame(self):
        """(fin, opcode, payload) for one frame.  Raises ValueError for an oversized one."""
        head = self._exactly(2)
        fin = bool(head[0] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._exactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._exactly(8))[0]
        if length > MAX_FRAME:
            raise ValueError("frame of %d bytes exceeds the %d byte cap" % (length, MAX_FRAME))
        return fin, head[0] & 0x0F, self._exactly(length)


def send_frame(s, opcode, payload=b""):
    """One masked frame (a client must mask)."""
    header = bytearray([0x80 | opcode])
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


def read_message(s, reader, timeout=INITIALIZE_TIMEOUT):
    """The next complete text message:  answers pings, joins fragments, stops on close.

    Returns (text, None) or (None, reason)."""
    s.settimeout(timeout)
    parts = []
    try:
        while True:
            fin, opcode, payload = reader.frame()
            if opcode == 0x9:  # ping
                send_frame(s, 0xA, payload)
            elif opcode == 0xA:  # pong
                continue
            elif opcode == 0x8:
                return None, "the server closed the connection"
            elif opcode in (0x1, 0x0):
                parts.append(payload)
                if sum(len(p) for p in parts) > MAX_FRAME:
                    return None, "message exceeds the %d byte cap" % MAX_FRAME
                if fin:
                    return b"".join(parts).decode("utf-8", "replace"), None
            # binary and unknown opcodes are ignored
    except TimeoutError:
        return None, "no reply within %ds" % timeout
    except (EOFError, ValueError, OSError) as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def close_cleanly(s):
    try:
        send_frame(s, 0x8, struct.pack(">H", 1000))
    except OSError:
        pass
    s.close()


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
    send_text(
        s,
        json.dumps(
            {
                "id": "lifecycle-1",
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientInfo": {"name": "mcode-probe"},
                    "capabilities": {
                        "desktopService": {"transportProtocolVersion": TRANSPORT_PROTOCOL_VERSION}
                    },
                },
            }
        ),
    )
    text, problem = read_message(s, Reader(s, rest))
    if problem:
        print("no initialize reply: %s" % problem)
        close_cleanly(s)
        return 3
    try:
        reply = json.loads(text)
    except (ValueError, RecursionError):
        print("the initialize reply is not valid JSON (%d bytes)" % len(text))
        close_cleanly(s)
        return 3
    if not isinstance(reply, dict) or reply.get("id") != "lifecycle-1":
        print("the reply is not the initialize response (wrong shape or id)")
        close_cleanly(s)
        return 3
    error = reply.get("error")
    if error is not None:
        if isinstance(error, dict):
            print(
                "initialize failed: code %s, message %s"
                % (error.get("code"), str(error.get("message"))[:200])
            )
        else:
            print("initialize failed")
        close_cleanly(s)
        return 3
    result = reply.get("result")
    if not isinstance(result, dict):
        print("the initialize reply has no result object")
        close_cleanly(s)
        return 3
    print("--- initialize reply ---")
    capabilities = result.get("capabilities")
    methods = capabilities.get("methods") if isinstance(capabilities, dict) else None
    methods = [m for m in methods if isinstance(m, str)] if isinstance(methods, list) else []
    print("--- the server's method catalogue (%d) ---" % len(methods))
    for name in methods:
        print("   ", name[:200])
    send_text(s, json.dumps({"method": "initialized"}))
    close_cleanly(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
