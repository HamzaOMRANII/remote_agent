"""
Minimal WebSocket client (RFC 6455) using only the Python standard library, so
the agent needs no pip install — it runs anywhere Python 3.8+ is present.

Scope: a client that connects (ws:// or wss://), sends text frames (masked, as
clients must), receives text frames, answers pings, and handles close. Enough
for the agent's control channel. Not a general-purpose library.
"""

import base64
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    pass


class WebSocketClient:
    def __init__(self, url, insecure=False, timeout=40):
        self.url = url
        self.insecure = insecure
        self.timeout = timeout
        self.sock = None

    def connect(self):
        u = urlparse(self.url)
        secure = u.scheme == "wss"
        host = u.hostname
        port = u.port or (443 if secure else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        raw = socket.create_connection((host, port), timeout=self.timeout)
        if secure:
            ctx = ssl.create_default_context()
            if self.insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())

        # read the handshake response headers
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            buf += chunk
            if len(buf) > 65536:
                raise WebSocketError("handshake too large")
        status = buf.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise WebSocketError(f"handshake failed: {status}")
        # keep the connection in blocking mode; frames arrive as sent
        self.sock.settimeout(None)

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _send_frame(self, opcode, payload=b""):
        b1 = 0x80 | opcode  # FIN + opcode
        length = len(payload)
        header = struct.pack("!B", b1)
        mask_bit = 0x80  # clients must mask
        if length < 126:
            header += struct.pack("!B", mask_bit | length)
        elif length < 65536:
            header += struct.pack("!B", mask_bit | 126) + struct.pack("!H", length)
        else:
            header += struct.pack("!B", mask_bit | 127) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send(self, text):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def _pong(self, payload):
        self._send_frame(OP_PONG, payload)

    # Returns the next text message as a str, or None when the socket closes.
    # Pings are answered transparently; continuation frames are reassembled.
    def recv(self):
        frags = []
        frag_op = None
        while True:
            head = self._recv_exact(2)
            if head is None:
                return None
            b1, b2 = head[0], head[1]
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                ext = self._recv_exact(2)
                if ext is None:
                    return None
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = self._recv_exact(8)
                if ext is None:
                    return None
                length = struct.unpack("!Q", ext)[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if payload is None:
                return None
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == OP_PING:
                self._pong(payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, payload[:2] if len(payload) >= 2 else b"")
                except Exception:
                    pass
                return None
            if opcode in (OP_TEXT, OP_BIN):
                if fin:
                    return payload.decode("utf-8", errors="replace")
                frags = [payload]
                frag_op = opcode
            elif opcode == OP_CONT:
                frags.append(payload)
                if fin:
                    return b"".join(frags).decode("utf-8", errors="replace")

    def close(self):
        if self.sock:
            try:
                self._send_frame(OP_CLOSE, b"")
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
