"""
Minimal WebSocket client using only Python stdlib (socket, threading, hashlib).
Implements just enough of RFC 6455 for the SublimeReview plugin:
  - Text frames in both directions
  - Ping/pong keepalive
  - Clean close handshake
"""

import base64
import hashlib
import os
import socket
import struct
import threading
from typing import Callable, Optional


# Opcodes
_OP_CONT  = 0x0
_OP_TEXT  = 0x1
_OP_BIN   = 0x2
_OP_CLOSE = 0x8
_OP_PING  = 0x9
_OP_PONG  = 0xA


def _make_key() -> str:
    return base64.b64encode(os.urandom(16)).decode()


def _accept_key(key: str) -> str:
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    sha = hashlib.sha1((key + GUID).encode()).digest()
    return base64.b64encode(sha).decode()


def _mask(payload: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _encode_frame(opcode: int, payload: bytes) -> bytes:
    """Build a masked client→server frame."""
    length = len(payload)
    header = bytearray()
    header.append(0x80 | opcode)   # FIN + opcode

    mask_bit = 0x80
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header += struct.pack(">H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack(">Q", length)

    mask_key = os.urandom(4)
    header += mask_key
    header += _mask(payload, mask_key)
    return bytes(header)


def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
    """Read one frame from the socket. Returns (opcode, payload)."""
    def recv_exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed")
            buf += chunk
        return buf

    b0, b1 = recv_exact(2)
    # b0: FIN(1) RSV(3) opcode(4)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F

    if length == 126:
        length = struct.unpack(">H", recv_exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(8))[0]

    mask_key = recv_exact(4) if masked else b""
    payload = recv_exact(length)
    if masked:
        payload = _mask(payload, mask_key)
    return opcode, payload


class WebSocketClient:
    """
    Simple WebSocket client.

    on_message(text: str) is called from a background thread for each
    incoming text frame.
    on_close() is called when the connection drops.
    """

    def __init__(
        self,
        url: str,
        on_message: Callable[[str], None],
        on_open: Optional[Callable[[], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self._url = url
        self._on_message = on_message
        self._on_open = on_open
        self._on_close = on_close
        self._on_error = on_error

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._closed = False

    # ── Public API ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Connect and start the receive loop in a daemon thread."""
        self._closed = False
        host, port = self._parse_url()
        self._sock = socket.create_connection((host, port), timeout=10)
        self._sock.settimeout(None)   # blocking from here on
        self._handshake(host, port)
        if self._on_open:
            self._on_open()
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        frame = _encode_frame(_OP_TEXT, payload)
        with self._send_lock:
            if self._sock:
                self._sock.sendall(frame)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._sock:
                self._sock.sendall(_encode_frame(_OP_CLOSE, b""))
                self._sock.close()
        except Exception:
            pass
        self._sock = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _parse_url(self) -> tuple[str, int]:
        # ws://host:port  or  ws://host
        url = self._url
        if url.startswith("ws://"):
            url = url[5:]
        if ":" in url:
            host, port_s = url.split(":", 1)
            return host, int(port_s)
        return url, 80

    def _handshake(self, host: str, port: int) -> None:
        key = _make_key()
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self._sock.sendall(request.encode())

        # Read response headers
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed during handshake")
            response += chunk

        if b"101" not in response.split(b"\r\n")[0]:
            raise ConnectionError(f"unexpected handshake response: {response[:200]}")

        expected = _accept_key(key)
        if expected.encode() not in response:
            raise ConnectionError("Sec-WebSocket-Accept mismatch")

    def _recv_loop(self) -> None:
        try:
            while not self._closed:
                opcode, payload = _read_frame(self._sock)

                if opcode in (_OP_TEXT, _OP_BIN):
                    self._on_message(payload.decode("utf-8", errors="replace"))

                elif opcode == _OP_PING:
                    with self._send_lock:
                        if self._sock:
                            self._sock.sendall(_encode_frame(_OP_PONG, payload))

                elif opcode == _OP_CLOSE:
                    break

                elif opcode == _OP_CONT:
                    pass   # fragmentation not needed for this use case

        except Exception as e:
            if not self._closed and self._on_error:
                self._on_error(e)
        finally:
            self._closed = True
            self._sock = None
            if self._on_close:
                self._on_close()
