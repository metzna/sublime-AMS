"""SublimeReview – Sublime Text 4 plugin (single-file, no external deps)."""
from __future__ import annotations

# ─── stdlib ──────────────────────────────────────────────────────────────────
import base64
import difflib
import hashlib
import json
import os
import socket
import struct
import threading
from typing import Callable, Optional

# ─── Sublime ──────────────────────────────────────────────────────────────────
import sublime
import sublime_plugin

# ═══════════════════════════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════════════════════════

SETTINGS_FILE = "SublimeReview.sublime-settings"


def _s(key: str, default=None):
    return sublime.load_settings(SETTINGS_FILE).get(key, default)


def _server_host() -> str:    return _s("server_host", "localhost")
def _server_port() -> int:    return int(_s("server_port", 9877))
def _auto_reconnect() -> bool: return bool(_s("auto_reconnect", True))
def _reconnect_delay() -> int: return int(_s("reconnect_delay", 3))
def _context_lines() -> int:  return int(_s("diff_context_lines", 3))
def _status_prefix() -> str:  return _s("status_bar_prefix", "Claude Review")
def _lock_icon() -> str:      return _s("lock_icon", "🔒")


# ═══════════════════════════════════════════════════════════════════════════════
# Minimal WebSocket client (RFC 6455, stdlib only)
# ═══════════════════════════════════════════════════════════════════════════════

_OP_TEXT  = 0x1
_OP_CLOSE = 0x8
_OP_PING  = 0x9
_OP_PONG  = 0xA


def _ws_mask(payload: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _ws_frame(opcode: int, payload: bytes) -> bytes:
    n = len(payload)
    hdr = bytearray([0x80 | opcode])
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
    key = os.urandom(4)
    return bytes(hdr) + key + _ws_mask(payload, key)


def _ws_read(sock: socket.socket) -> tuple:
    def exact(n):
        buf = b""
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                raise ConnectionError("socket closed")
            buf += c
        return buf

    b0, b1 = exact(2)
    opcode  = b0 & 0x0F
    masked  = bool(b1 & 0x80)
    length  = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", exact(8))[0]
    mkey    = exact(4) if masked else b""
    payload = exact(length)
    return opcode, (_ws_mask(payload, mkey) if masked else payload)


class _WSClient:
    def __init__(self, url: str, on_message, on_open=None, on_close=None, on_error=None):
        self._url       = url
        self._on_msg    = on_message
        self._on_open   = on_open
        self._on_close  = on_close
        self._on_err    = on_error
        self._sock: Optional[socket.socket] = None
        self._lock      = threading.Lock()
        self._closed    = False

    def connect(self):
        self._closed = False
        host, port  = self._parse()
        self._sock  = socket.create_connection((host, port), timeout=10)
        self._sock.settimeout(None)
        self._handshake(host, port)
        if self._on_open:
            self._on_open()
        threading.Thread(target=self._loop, daemon=True).start()

    def send(self, text: str):
        with self._lock:
            if self._sock:
                self._sock.sendall(_ws_frame(_OP_TEXT, text.encode()))

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._sock:
                self._sock.sendall(_ws_frame(_OP_CLOSE, b""))
                self._sock.close()
        except Exception:
            pass
        self._sock = None

    def _parse(self):
        u = self._url[5:] if self._url.startswith("ws://") else self._url
        h, _, p = u.partition(":")
        return h, int(p) if p else 80

    def _handshake(self, host, port):
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall((
            f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            resp += chunk
        if b"101" not in resp.split(b"\r\n")[0]:
            raise ConnectionError(f"bad handshake: {resp[:80]}")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if expected.encode() not in resp:
            raise ConnectionError("Sec-WebSocket-Accept mismatch")

    def _loop(self):
        try:
            while not self._closed:
                opcode, payload = _ws_read(self._sock)
                if opcode in (_OP_TEXT, 0x2):
                    self._on_msg(payload.decode("utf-8", errors="replace"))
                elif opcode == _OP_PING:
                    with self._lock:
                        if self._sock:
                            self._sock.sendall(_ws_frame(_OP_PONG, payload))
                elif opcode == _OP_CLOSE:
                    break
        except Exception as e:
            if not self._closed and self._on_err:
                self._on_err(e)
        finally:
            self._closed = True
            self._sock = None
            if self._on_close:
                self._on_close()


# ═══════════════════════════════════════════════════════════════════════════════
# Diff panel
# ═══════════════════════════════════════════════════════════════════════════════

PANEL_NAME = "sublime_review_diff"

_SCOPE = {
    "add":  "markup.inserted",
    "del":  "markup.deleted",
    "hdr":  "markup.changed",
    "ctx":  "comment",
}


def _diff_lines(review: dict) -> list:
    tool      = review.get("tool_name", "Edit")
    fp        = review.get("file_path", "")
    old       = review.get("old_string", "")
    new       = review.get("new_string", "")
    content   = review.get("content", "")
    ctx       = _context_lines()

    if tool == "Write":
        lines = [("--- (new file)\n", "hdr"), (f"+++ {fp}\n", "hdr")]
        for ln in content.splitlines(keepends=True):
            lines.append((f"+{ln}", "add"))
        return lines

    old_l = old.splitlines(keepends=True)
    new_l = new.splitlines(keepends=True)
    raw   = list(difflib.unified_diff(old_l, new_l, fromfile=f"a/{fp}", tofile=f"b/{fp}", n=ctx))
    if not raw:
        return [("(no changes)\n", "ctx")]

    result = []
    for dl in raw:
        if dl.startswith(("---", "+++", "@@")):
            result.append((dl, "hdr"))
        elif dl.startswith("+"):
            result.append((dl, "add"))
        elif dl.startswith("-"):
            result.append((dl, "del"))
        else:
            result.append((dl, "ctx"))
    return result


class _ReviewPanel:
    def __init__(self, window: sublime.Window):
        self._window = window
        self._view: Optional[sublime.View] = None

    def _view_(self) -> sublime.View:
        if not self._view or not self._view.is_valid():
            v = self._window.create_output_panel(PANEL_NAME)
            v.set_read_only(False)
            v.settings().set("sublime_review_panel", True)
            v.settings().set("gutter", True)
            v.settings().set("line_numbers", False)
            v.settings().set("word_wrap", False)
            v.settings().set("draw_white_space", "none")
            v.set_read_only(True)
            self._view = v
        return self._view

    def show(self, review: dict):
        v = self._view_()
        v.set_read_only(False)
        v.run_command("select_all")
        v.run_command("right_delete")

        agent = review.get("agent_label", "")
        tool  = review.get("tool_name", "")
        fp    = review.get("file_path", "")
        pos   = review.get("queue_position", 1)
        total = review.get("queue_total", 1)

        header = (
            f"{'─' * 60}\n"
            f"  {agent}  │  {tool}  │  {pos}/{total} in queue\n"
            f"  {fp}\n"
            f"  [Enter] Accept   [Escape] Reject   [Tab] Next\n"
            f"{'─' * 60}\n"
        )
        v.run_command("append", {"characters": header, "force": True})

        for text, role in _diff_lines(review):
            start = v.size()
            v.run_command("append", {"characters": text, "force": True})
            v.add_regions(
                f"sr_{role}_{start}",
                [sublime.Region(start, v.size())],
                _SCOPE[role], "",
                sublime.DRAW_NO_OUTLINE,
            )

        v.set_read_only(True)
        v.settings().set("sublime_review_panel_focused", True)
        self._window.run_command("show_panel", {"panel": f"output.{PANEL_NAME}"})

    def hide(self):
        self._window.run_command("hide_panel", {"panel": f"output.{PANEL_NAME}"})

    def clear(self):
        if self._view and self._view.is_valid():
            self._view.set_read_only(False)
            self._view.run_command("select_all")
            self._view.run_command("right_delete")
            self._view.set_read_only(True)
        self.hide()


# ═══════════════════════════════════════════════════════════════════════════════
# Lock / status indicator
# ═══════════════════════════════════════════════════════════════════════════════

class _LockIndicator:
    def __init__(self, window: sublime.Window):
        self._window = window
        self._locked: dict = {}   # file_path → original tab name

    def apply(self, locks: dict):
        cur  = set(locks)
        prev = set(self._locked)
        for fp in cur  - prev: self._lock(fp)
        for fp in prev - cur:  self._unlock(fp)

    def clear(self):
        for fp in list(self._locked): self._unlock(fp)

    def status(self, pending: int, waiting: int = 0):
        if pending == 0:
            for v in self._window.views(): v.erase_status("sublime_review")
        else:
            msg = f"{_status_prefix()}: {pending} pending"
            if waiting: msg += f" ({waiting} waiting)"
            for v in self._window.views(): v.set_status("sublime_review", msg)

    def _find(self, fp: str) -> Optional[sublime.View]:
        for v in self._window.views():
            if v.file_name() == fp: return v
        return None

    def _lock(self, fp: str):
        v = self._find(fp)
        if v:
            orig = v.name() or v.file_name() or fp
            self._locked[fp] = orig
            v.set_name(f"{_lock_icon()} {orig}")
        else:
            self._locked[fp] = None

    def _unlock(self, fp: str):
        orig = self._locked.pop(fp, None)
        v    = self._find(fp)
        if v:
            v.set_name(orig if orig is not None else "")


# ═══════════════════════════════════════════════════════════════════════════════
# Review manager
# ═══════════════════════════════════════════════════════════════════════════════

_managers: dict = {}   # window_id → _Manager


def _manager(window: Optional[sublime.Window] = None) -> Optional["_Manager"]:
    if window is None:
        window = sublime.active_window()
    if window is None:
        return None
    wid = window.id()
    if wid not in _managers:
        m = _Manager(window)
        _managers[wid] = m
        m.start()
    return _managers[wid]


class _Manager:
    def __init__(self, window: sublime.Window):
        self._window  = window
        self._panel   = _ReviewPanel(window)
        self._ind     = _LockIndicator(window)
        self._queue   = []
        self._active  = None
        self._mu      = threading.Lock()
        self._ws: Optional[_WSClient] = None
        self._running = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._connect()

    def stop(self):
        self._running = False
        if self._ws:
            try: self._ws.close()
            except Exception: pass
            self._ws = None
        self._ind.clear()
        self._ind.status(0)
        self._panel.clear()

    # ── WebSocket ─────────────────────────────────────────────────────────────

    def _connect(self):
        url = f"ws://{_server_host()}:{_server_port()}"
        self._ws = _WSClient(url,
            on_message=self._on_msg,
            on_open=self._on_open,
            on_close=self._on_close,
            on_error=self._on_err,
        )
        def _try():
            try:
                self._ws.connect()
            except Exception as e:
                sublime.set_timeout(
                    lambda: sublime.status_message(f"SublimeReview: connect failed – {e}"), 0)
                if self._running and _auto_reconnect():
                    sublime.set_timeout_async(self._reconnect, _reconnect_delay() * 1000)
        threading.Thread(target=_try, daemon=True).start()

    def _on_open(self):
        sublime.set_timeout(
            lambda: sublime.status_message("SublimeReview: connected"), 0)

    def _on_msg(self, raw: str):
        try:   msg = json.loads(raw)
        except Exception: return
        t = msg.get("type")
        if t == "review_request":
            sublime.set_timeout(lambda: self._enqueue(msg), 0)
        elif t == "lock_update":
            lk = msg.get("locks", {})
            sublime.set_timeout(lambda: self._ind.apply(lk), 0)
        elif t == "queue_update":
            n = msg.get("queue_total", 0)
            sublime.set_timeout(lambda: self._ind.status(n), 0)

    def _on_err(self, e: Exception):
        sublime.set_timeout(
            lambda: sublime.status_message(f"SublimeReview: WS error – {e}"), 0)

    def _on_close(self):
        if self._running and _auto_reconnect():
            sublime.set_timeout_async(self._reconnect, _reconnect_delay() * 1000)

    def _reconnect(self):
        if self._running: self._connect()

    # ── queue ─────────────────────────────────────────────────────────────────

    def _enqueue(self, review: dict):
        with self._mu:
            self._queue.append(review)
            if self._active is None:
                self._next()
            else:
                self._refresh()

    def _next(self):
        with self._mu:
            if not self._queue:
                self._active = None
                self._panel.clear()
                self._ind.status(0)
                return
            self._active = self._queue.pop(0)
        fp = self._active.get("file_path", "")
        if fp: self._window.open_file(fp, sublime.TRANSIENT)
        self._panel.show(self._active)
        self._refresh()

    def _refresh(self):
        with self._mu:
            w = len(self._queue)
            p = (1 if self._active else 0) + w
        self._ind.status(p, w)

    # ── decisions ─────────────────────────────────────────────────────────────

    def accept(self): self._decide("allow")
    def reject(self): self._decide("deny")

    def _decide(self, decision: str):
        with self._mu: review = self._active
        if not review: return
        self._send({"type": "review_decision",
                    "review_id": review.get("review_id"),
                    "decision": decision})
        sublime.set_timeout(self._next, 0)

    def cycle(self):
        with self._mu:
            if not self._queue: return
            if self._active: self._queue.append(self._active)
            self._active = None
        self._next()

    def unlock(self, fp: str):
        self._send({"type": "unlock_file", "file_path": fp})

    def _send(self, msg: dict):
        if self._ws:
            try: self._ws.send(json.dumps(msg))
            except Exception as e:
                sublime.set_timeout(
                    lambda: sublime.status_message(f"SublimeReview: send error – {e}"), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════════════════

class SublimeReviewAcceptCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = _manager(self.window)
        if m: m.accept()


class SublimeReviewRejectCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = _manager(self.window)
        if m: m.reject()


class SublimeReviewNextCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = _manager(self.window)
        if m: m.cycle()


class SublimeReviewUnlockFileCommand(sublime_plugin.WindowCommand):
    def run(self, file_path=""):
        if not file_path:
            v = self.window.active_view()
            file_path = v.file_name() if v else ""
        if not file_path:
            sublime.status_message("SublimeReview: no file")
            return
        m = _manager(self.window)
        if m: m.unlock(file_path)


class SublimeReviewConnectCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = _manager(self.window)
        if m:
            m.stop(); m.start()
            sublime.status_message("SublimeReview: reconnecting…")


# ═══════════════════════════════════════════════════════════════════════════════
# Event listeners
# ═══════════════════════════════════════════════════════════════════════════════

class SublimeReviewListener(sublime_plugin.EventListener):
    def on_activated(self, view):
        w = view.window()
        if w: _manager(w)

    def on_pre_close_window(self, window):
        m = _managers.pop(window.id(), None)
        if m: m.stop()


class SublimeReviewPanelContext(sublime_plugin.EventListener):
    def on_query_context(self, view, key, operator, operand, match_all):
        if key != "sublime_review_panel_focused":
            return None
        val = view.settings().get("sublime_review_panel", False)
        if operator == sublime.OP_EQUAL:     return val == operand
        if operator == sublime.OP_NOT_EQUAL: return val != operand
        return None
