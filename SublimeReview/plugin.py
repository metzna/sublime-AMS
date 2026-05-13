# SublimeReview - Claude Code review plugin for Sublime Text
# Python 3.3 compatible (no type hints, no f-strings, no typing module)

import base64
import difflib
import hashlib
import json
import os
import socket
import struct
import threading

import sublime
import sublime_plugin

# ===============================================================================
# Settings
# ===============================================================================

SETTINGS_FILE = "SublimeReview.sublime-settings"


def _s(key, default=None):
    return sublime.load_settings(SETTINGS_FILE).get(key, default)


def _host():            return _s("server_host", "localhost")
def _port():            return int(_s("server_port", 9877))
def _auto_reconnect():  return bool(_s("auto_reconnect", True))
def _reconnect_delay(): return int(_s("reconnect_delay", 3))
def _context_lines():   return int(_s("diff_context_lines", 3))
def _status_prefix():   return _s("status_bar_prefix", "Claude Review")
def _lock_icon():       return _s("lock_icon", "[locked]")


# ===============================================================================
# WebSocket client (RFC 6455, stdlib only)
# ===============================================================================

_OP_TEXT  = 0x1
_OP_CLOSE = 0x8
_OP_PING  = 0x9
_OP_PONG  = 0xA


def _mask(payload, key):
    return bytes(bytearray(b ^ key[i % 4] for i, b in enumerate(bytearray(payload))))


def _frame(opcode, payload):
    n = len(payload)
    hdr = bytearray([0x80 | opcode])
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126)
        hdr += bytearray(struct.pack(">H", n))
    else:
        hdr.append(0x80 | 127)
        hdr += bytearray(struct.pack(">Q", n))
    key = os.urandom(4)
    return bytes(hdr) + key + _mask(payload, key)


def _read_frame(sock):
    def recv(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed")
            buf += chunk
        return buf

    header = recv(2)
    b0 = header[0] if isinstance(header[0], int) else ord(header[0])
    b1 = header[1] if isinstance(header[1], int) else ord(header[1])
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv(8))[0]
    mkey    = recv(4) if masked else b""
    payload = recv(length)
    return opcode, (_mask(payload, mkey) if masked else payload)


class _WSClient(object):
    def __init__(self, url, on_message, on_open=None, on_close=None, on_error=None):
        self._url      = url
        self._on_msg   = on_message
        self._on_open  = on_open
        self._on_close = on_close
        self._on_err   = on_error
        self._sock     = None
        self._lock     = threading.Lock()
        self._closed   = False

    def connect(self):
        self._closed = False
        host, port   = self._parse()
        self._sock   = socket.create_connection((host, port), timeout=10)
        self._sock.settimeout(None)
        self._handshake(host, port)
        if self._on_open:
            self._on_open()
        t = threading.Thread(target=self._loop)
        t.daemon = True
        t.start()

    def send(self, text):
        payload = text.encode("utf-8")
        with self._lock:
            if self._sock:
                try:
                    self._sock.sendall(_frame(_OP_TEXT, payload))
                except Exception:
                    pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._sock:
                self._sock.sendall(_frame(_OP_CLOSE, b""))
                self._sock.close()
        except Exception:
            pass
        self._sock = None

    def _parse(self):
        u = self._url[5:] if self._url.startswith("ws://") else self._url
        if ":" in u:
            h, p = u.rsplit(":", 1)
            return h, int(p)
        return u, 80

    def _handshake(self, host, port):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET / HTTP/1.1\r\nHost: {0}:{1}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: {2}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).format(host, port, key)
        self._sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            resp += chunk
        first_line = resp.split(b"\r\n")[0]
        if b"101" not in first_line:
            raise ConnectionError("bad handshake: " + repr(resp[:80]))
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if expected.encode() not in resp:
            raise ConnectionError("Sec-WebSocket-Accept mismatch")

    def _loop(self):
        try:
            while not self._closed:
                opcode, payload = _read_frame(self._sock)
                if opcode in (_OP_TEXT, 0x2):
                    self._on_msg(payload.decode("utf-8", errors="replace"))
                elif opcode == _OP_PING:
                    with self._lock:
                        if self._sock:
                            self._sock.sendall(_frame(_OP_PONG, payload))
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


# ===============================================================================
# Diff rendering (plain text only - no add_regions to avoid crashes)
# ===============================================================================

PANEL_NAME = "sublime_review_diff"


def _build_diff(review):
    tool    = review.get("tool_name", "Edit")
    fp      = review.get("file_path", "")
    old     = review.get("old_string", "")
    new     = review.get("new_string", "")
    content = review.get("content", "")
    ctx     = _context_lines()

    if tool == "Write":
        lines = ["--- (new file)", "+++ " + fp]
        for ln in content.splitlines():
            lines.append("+" + ln)
        return "\n".join(lines)

    old_l = old.splitlines(True)
    new_l = new.splitlines(True)
    raw   = list(difflib.unified_diff(
        old_l, new_l,
        fromfile="a/" + fp,
        tofile="b/" + fp,
        n=ctx,
    ))
    if not raw:
        return "(no changes)"
    return "".join(raw)




class SublimeReviewSetContentCommand(sublime_plugin.TextCommand):
    """Internal command: replace entire view content."""
    def run(self, edit, text=""):
        self.view.replace(edit, sublime.Region(0, self.view.size()), text)

class _ReviewPanel(object):
    def __init__(self, window):
        self._window = window
        self._view   = None

    def _get_view(self):
        v = self._window.create_output_panel(PANEL_NAME)
        v.settings().set("sublime_review_panel", True)
        v.settings().set("gutter", False)
        v.settings().set("line_numbers", False)
        v.settings().set("word_wrap", False)
        self._view = v
        return v

    def show(self, review):
        try:
            v = self._get_view()
            v.set_read_only(False)
            v.run_command("select_all")
            v.run_command("right_delete")

            agent = review.get("agent_label", "")
            tool  = review.get("tool_name", "")
            fp    = review.get("file_path", "")
            pos   = review.get("queue_position", 1)
            total = review.get("queue_total", 1)
            sep   = "-" * 60

            text = (
                "{sep}\n"
                "  {agent}  |  {tool}  |  {pos}/{total} in queue\n"
                "  {fp}\n"
                "  [Enter] Accept   [Escape] Reject   [Tab] Next\n"
                "{sep}\n"
                "{diff}\n"
            ).format(
                sep=sep, agent=agent, tool=tool,
                pos=pos, total=total, fp=fp,
                diff=_build_diff(review),
            )

            v.run_command("sublime_review_set_content", {"text": text})
            v.set_read_only(True)
            v.settings().set("sublime_review_panel_focused", True)
            self._window.run_command("show_panel", {"panel": "output." + PANEL_NAME})
        except Exception as e:
            sublime.status_message("SublimeReview panel error: " + str(e))


    def _colorize(self, v, text):
        add_regs = []
        del_regs = []
        hdr_regs = []
        pos = 0
        for line in text.split("\n"):
            end = pos + len(line)
            reg = sublime.Region(pos, end)
            if line.startswith("+") and not line.startswith("+++"):
                add_regs.append(reg)
            elif line.startswith("-") and not line.startswith("---"):
                del_regs.append(reg)
            elif line.startswith(("@@", "---", "+++")):
                hdr_regs.append(reg)
            pos = end + 1
        v.erase_regions("sr_add")
        v.erase_regions("sr_del")
        v.erase_regions("sr_hdr")
        if add_regs:
            v.add_regions("sr_add", add_regs, "markup.inserted", "", sublime.DRAW_NO_OUTLINE)
        if del_regs:
            v.add_regions("sr_del", del_regs, "markup.deleted", "", sublime.DRAW_NO_OUTLINE)
        if hdr_regs:
            v.add_regions("sr_hdr", hdr_regs, "markup.changed", "", sublime.DRAW_NO_OUTLINE)

    def clear(self):
        try:
            if self._view and self._view.is_valid():
                self._view.set_read_only(False)
                self._view.run_command("select_all")
                self._view.run_command("right_delete")
                self._view.set_read_only(True)
            self._window.run_command("hide_panel", {"panel": "output." + PANEL_NAME})
        except Exception:
            pass


# ===============================================================================
# Lock / status indicator
# ===============================================================================

class _LockIndicator(object):
    def __init__(self, window):
        self._window = window
        self._locked = {}

    def apply(self, locks):
        cur  = set(locks.keys())
        prev = set(self._locked.keys())
        for fp in cur  - prev: self._lock(fp)
        for fp in prev - cur:  self._unlock(fp)

    def clear(self):
        for fp in list(self._locked.keys()):
            self._unlock(fp)

    def set_status(self, pending, waiting=0):
        if pending == 0:
            for v in self._window.views():
                v.erase_status("sublime_review")
        else:
            msg = "{0}: {1} pending".format(_status_prefix(), pending)
            if waiting:
                msg += " ({0} waiting)".format(waiting)
            for v in self._window.views():
                v.set_status("sublime_review", msg)

    def _lock(self, fp):
        self._locked[fp] = True

    def _unlock(self, fp):
        self._locked.pop(fp, None)


# ===============================================================================
# Review manager
# ===============================================================================

_managers = {}


def _manager(window=None):
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


class _Manager(object):
    def __init__(self, window):
        self._window  = window
        self._panel   = _ReviewPanel(window)
        self._ind     = _LockIndicator(window)
        self._queue   = []
        self._active  = None
        self._mu      = threading.Lock()
        self._ws      = None
        self._running = False

    def start(self):
        self._running = True
        self._connect()

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._ind.clear()
        self._ind.set_status(0)
        self._panel.clear()

    def _connect(self):
        url = "ws://{0}:{1}".format(_host(), _port())
        self._ws = _WSClient(
            url,
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
                    lambda: sublime.status_message("SublimeReview: connect failed - " + str(e)), 0)
                if self._running and _auto_reconnect():
                    sublime.set_timeout_async(self._reconnect, _reconnect_delay() * 1000)
        t = threading.Thread(target=_try)
        t.daemon = True
        t.start()

    def _on_open(self):
        sublime.set_timeout(
            lambda: sublime.status_message("SublimeReview: connected"), 0)

    def _on_msg(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        t = msg.get("type")
        if t == "review_request":
            sublime.set_timeout(lambda: self._enqueue(msg), 0)
        elif t == "lock_update":
            lk = msg.get("locks", {})
            sublime.set_timeout(lambda: self._ind.apply(lk), 0)
        elif t == "queue_update":
            n = msg.get("queue_total", 0)
            sublime.set_timeout(lambda: self._ind.set_status(n), 0)

    def _on_err(self, e):
        sublime.set_timeout(
            lambda: sublime.status_message("SublimeReview: error - " + str(e)), 0)

    def _on_close(self):
        if self._running and _auto_reconnect():
            sublime.set_timeout_async(self._reconnect, _reconnect_delay() * 1000)

    def _reconnect(self):
        if self._running:
            self._connect()

    def _enqueue(self, review):
        with self._mu:
            self._queue.append(review)
            has_active = self._active is not None
        if not has_active:
            self._next()
        else:
            self._refresh()

    def _next(self):
        with self._mu:
            if not self._queue:
                self._active = None
            else:
                self._active = self._queue.pop(0)
            review = self._active

        if review is None:
            self._panel.clear()
            self._ind.set_status(0)
            return

        self._panel.show(review)
        self._refresh()

    def _refresh(self):
        with self._mu:
            w = len(self._queue)
            a = self._active is not None
        self._ind.set_status((1 if a else 0) + w, w)

    def accept(self):
        self._decide("allow")

    def reject(self):
        self._decide("deny")

    def _decide(self, decision):
        with self._mu:
            review = self._active
        if review is None:
            return
        self._send({
            "type": "review_decision",
            "review_id": review.get("review_id"),
            "decision": decision,
        })
        sublime.set_timeout(self._next, 0)

    def cycle(self):
        with self._mu:
            if not self._queue:
                return
            if self._active:
                self._queue.append(self._active)
            self._active = None
        self._next()

    def unlock_file(self, fp):
        self._send({"type": "unlock_file", "file_path": fp})

    def _send(self, msg):
        if self._ws:
            try:
                self._ws.send(json.dumps(msg))
            except Exception as e:
                sublime.set_timeout(
                    lambda: sublime.status_message("SublimeReview: send error - " + str(e)), 0)


# ===============================================================================
# Commands
# ===============================================================================

class SublimeReviewAcceptCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = _manager(self.window)
        if m:
            m.accept()


class SublimeReviewRejectCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = _manager(self.window)
        if m:
            m.reject()


class SublimeReviewNextCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = _manager(self.window)
        if m:
            m.cycle()


class SublimeReviewUnlockFileCommand(sublime_plugin.WindowCommand):
    def run(self, file_path=""):
        if not file_path:
            v = self.window.active_view()
            file_path = v.file_name() if v else ""
        if not file_path:
            sublime.status_message("SublimeReview: no file selected")
            return
        m = _manager(self.window)
        if m:
            m.unlock_file(file_path)


class SublimeReviewConnectCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = _manager(self.window)
        if m:
            m.stop()
            m.start()
            sublime.status_message("SublimeReview: reconnecting...")


# ===============================================================================
# Event listeners
# ===============================================================================

class SublimeReviewListener(sublime_plugin.EventListener):
    def on_activated(self, view):
        w = view.window()
        if w:
            _manager(w)

    def on_pre_close_window(self, window):
        m = _managers.pop(window.id(), None)
        if m:
            m.stop()


class SublimeReviewPanelContext(sublime_plugin.EventListener):
    def on_query_context(self, view, key, operator, operand, match_all):
        if key != "sublime_review_panel_focused":
            return None
        val = bool(view.settings().get("sublime_review_panel", False))
        if operator == sublime.OP_EQUAL:
            return val == operand
        if operator == sublime.OP_NOT_EQUAL:
            return val != operand
        return None
