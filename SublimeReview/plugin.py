# SublimeReview – Claude Code review plugin for Sublime Text
#
# Shows a unified diff panel for every file edit Claude Code proposes.
# The user presses Enter (accept) or Escape (reject) in the panel.
#
# Architecture:
#   _WSClient      — minimal RFC 6455 WebSocket client (stdlib only, no deps)
#   _ReviewPanel   — output panel that renders the diff as plain text
#   _LockIndicator — status bar badge showing pending review count
#   _Manager       — per-window coordinator: WS connection, review queue, decisions
#   Commands       — SublimeReviewAccept/Reject/Next/UnlockFile/Connect
#   Listeners      — SublimeReviewListener (lifecycle), SublimeReviewPanelContext (keybindings)
#
# Python 3.3 compatible: no type annotations, no f-strings, no typing module.
# (Sublime Text 4 defaults to Python 3.3 for packages without .python-version.)

import base64
import difflib
import hashlib
import html as _html
import http.client
import json
import os
import socket
import struct
import subprocess
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
# Diff rendering
# ===============================================================================

PANEL_NAME = "sublime_review_diff"


def _agent_color(session_id):
    """Derive a stable accent color from a session ID via HSL with fixed S/L."""
    digest = hashlib.md5(session_id.encode("utf-8")).digest()
    hue = ((digest[0] << 8) | digest[1]) / 65536.0 * 360.0
    s, l = 0.70, 0.62  # vivid, readable on dark backgrounds
    c = (1.0 - abs(2.0 * l - 1.0)) * s
    x = c * (1.0 - abs((hue / 60.0) % 2.0 - 1.0))
    m = l - c / 2.0
    h = int(hue / 60) % 6
    if   h == 0: r, g, b = c, x, 0.0
    elif h == 1: r, g, b = x, c, 0.0
    elif h == 2: r, g, b = 0.0, c, x
    elif h == 3: r, g, b = 0.0, x, c
    elif h == 4: r, g, b = x, 0.0, c
    else:        r, g, b = c, 0.0, x
    return "#{:02x}{:02x}{:02x}".format(
        int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)
    )


# Build a unified diff string for the review payload.
# For Write calls the entire new content is shown as additions (+).
# For Edit/MultiEdit calls difflib produces a standard unified diff.
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


def _build_phantom_html(text, color):
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    line_bg  = "rgba({},{},{},0.12)".format(r, g, b)
    block_bg = "rgba({},{},{},0.08)".format(r, g, b)
    lines = "".join(
        '<div style="color:{c};background:{bg};'
        'font-family:monospace;white-space:pre-wrap;word-break:break-all;">{t}</div>'.format(
            c=color, bg=line_bg, t=_html.escape(ln)
        )
        for ln in (text.splitlines() or [""])
    )
    return (
        '<body id="sr_new">'
        '<div style="margin:2px 0;padding:4px 6px;'
        'border-left:3px solid {c};background:{bg};">'
        '{lines}</div></body>'
    ).format(c=color, bg=block_bg, lines=lines)


class SublimeReviewSetContentCommand(sublime_plugin.TextCommand):
    """Internal command: replace entire view content."""
    def run(self, edit, text=""):
        self.view.erase(edit, sublime.Region(0, self.view.size()))
        self.view.insert(edit, 0, text)

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

    def show(self, review, compact=False):
        try:
            # Show the panel first so find_output_panel returns the live view
            self._window.run_command("show_panel", {"panel": "output." + PANEL_NAME})
            v = self._window.find_output_panel(PANEL_NAME)
            if v is None:
                self._get_view()
                self._window.run_command("show_panel", {"panel": "output." + PANEL_NAME})
                v = self._window.find_output_panel(PANEL_NAME)
            if v is None:
                sublime.status_message("SublimeReview: could not get panel view")
                return

            agent   = review.get("agent_label", "")
            tool    = review.get("tool_name", "")
            fp      = review.get("file_path", "")
            pos     = review.get("queue_position", 1)
            total   = review.get("queue_total", 1)
            color   = _agent_color(review.get("session_id", ""))
            sep     = "-" * 60

            header = (
                "{sep}\n"
                "  {agent}  |  {tool}  |  {pos}/{total} in queue\n"
                "  {fp}\n"
                "  [Enter] Accept   [Escape] Reject   [Tab] Next\n"
                "{sep}\n"
            ).format(sep=sep, agent=agent, tool=tool, pos=pos, total=total, fp=fp)

            text = header if compact else header + _build_diff(review) + "\n"

            v.settings().set("sublime_review_panel", True)
            v.settings().set("sublime_review_panel_focused", True)
            v.set_read_only(False)
            v.run_command("sublime_review_set_content", {"text": text})
            v.set_read_only(True)
            v.show(0)

            agent_region = v.find(agent, 0, sublime.LITERAL)
            if agent_region.a != -1:
                v.add_regions("sr_agent", [agent_region], "", "",
                              sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE,
                              annotations=[agent], annotation_color=color)

            is_write = (tool == "Write")
            if is_write and not compact:
                v.assign_syntax("Packages/Diff/Diff.sublime-syntax")
            else:
                v.assign_syntax("Packages/Text/Plain text.tmLanguage")
                if not compact:
                    # Defer coloring so the view is fully rendered before add_regions runs
                    sublime.set_timeout(lambda: self._colorize(v), 30)
        except Exception as e:
            sublime.status_message("SublimeReview panel error: " + str(e))


    def _colorize(self, v):
        """Add green/red/grey region highlights to the diff lines.

        Uses view.lines() to get regions from the live view content rather
        than computing byte offsets manually, which avoids off-by-one errors
        from differing line endings.  Flags=0 draws solid background colour
        using the scope's colour scheme entry.
        """
        add_regs = []
        del_regs = []
        hdr_regs = []

        for region in v.lines(sublime.Region(0, v.size())):
            line = v.substr(region)
            if line.startswith("+") and not line.startswith("+++"):
                add_regs.append(region)
            elif line.startswith("-") and not line.startswith("---"):
                del_regs.append(region)
            elif line.startswith(("@@", "---", "+++")):
                hdr_regs.append(region)

        v.erase_regions("sr_add")
        v.erase_regions("sr_del")
        v.erase_regions("sr_hdr")

        if hdr_regs:
            v.add_regions("sr_hdr", hdr_regs, "markup.changed", "", 0)
        if del_regs:
            v.add_regions("sr_del", del_regs, "markup.deleted", "", 0)
        if add_regs:
            v.add_regions("sr_add", add_regs, "markup.inserted", "", 0)

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
# Inline diff (phantom-based, for Edit/MultiEdit)
# ===============================================================================

class _InlineDiff(object):
    """
    Shows the proposed change directly in the file view:
      - old_string region highlighted red
      - new_string rendered as a green HTML phantom below it
    Returns True from show() if the inline display was set up successfully,
    False if the file is not open or old_string was not found (caller falls
    back to the full diff panel in that case).
    """

    def __init__(self, window):
        self._window       = window
        self._phantom_set  = None
        self._phantom_view = None

    def show(self, review):
        fp    = review.get("file_path", "")
        old   = review.get("old_string", "")
        new   = review.get("new_string", "")
        color = _agent_color(review.get("session_id", ""))

        v = self._window.find_open_file(fp)
        if v is None:
            v = self._window.open_file(fp)
        if v is None or v.is_loading():
            return False

        regions = v.find_all(old, sublime.LITERAL)
        if not regions or len(regions) > 1:
            return False
        region = regions[0]

        v.add_regions("sr_old", [region], "markup.deleted", "", 0)

        self._phantom_view = v
        self._phantom_set  = sublime.PhantomSet(v, "sr_new")
        # Zero-width anchor on the last line of the deleted region.
        # LAYOUT_BLOCK anchors to the line containing region.begin(), so we
        # pass a point at region.end(). If the old_string captured a trailing
        # newline, region.end() sits on the next line — step back one char.
        end_pt = region.end()
        if end_pt > 0 and v.substr(end_pt - 1) == "\n":
            end_pt -= 1
        self._phantom_set.update([
            sublime.Phantom(sublime.Region(end_pt), _build_phantom_html(new, color), sublime.LAYOUT_BLOCK)
        ])

        self._window.focus_view(v)
        v.show(region, True)
        return True

    def clear(self):
        if self._phantom_set is not None:
            self._phantom_set.update([])
            self._phantom_set = None
        if self._phantom_view is not None and self._phantom_view.is_valid():
            self._phantom_view.erase_regions("sr_old")
        self._phantom_view = None


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
_server_proc = None
_server_start_lock = threading.Lock()


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


def _start_server_if_needed():
    global _server_proc
    with _server_start_lock:
        try:
            conn = http.client.HTTPConnection("localhost", 9876, timeout=1)
            conn.request("GET", "/status")
            conn.getresponse()
            return  # already running
        except Exception:
            pass
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sublime_review_server.py")
        if not os.path.exists(script):
            sublime.status_message("SublimeReview: server script not found: " + script)
            return
        try:
            _server_proc = subprocess.Popen(
                ["python3", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            sublime.status_message("SublimeReview: could not start server: " + str(e))



class _Manager(object):
    """
    Per-window coordinator.  One instance exists per Sublime window.

    Responsibilities:
      - Maintain the WebSocket connection to the review server.
      - Queue incoming review_request messages (FIFO).
      - Display one review at a time in the diff panel.
      - Send review_decision messages back to the server.
      - Handle review_cancelled messages from the server (auto-deny).
    """

    def __init__(self, window):
        self._window        = window
        self._panel         = _ReviewPanel(window)
        self._inline        = _InlineDiff(window)
        self._ind           = _LockIndicator(window)
        self._queue         = []
        self._active        = None
        self._mu            = threading.Lock()
        self._ws            = None
        self._running       = False
        self._cancelled_ids = set()

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
        self._inline.clear()
        self._panel.clear()
        self._cancelled_ids.clear()

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
        elif t == "review_cancelled":
            rid = msg.get("review_id")
            sublime.set_timeout(lambda: self._cancel(rid), 0)
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
            _start_server_if_needed()
            self._connect()

    def _enqueue(self, review):
        with self._mu:
            self._queue.append(review)
            has_active = self._active is not None
        if not has_active:
            self._next()
        else:
            self._refresh()

    def _cancel(self, review_id):
        with self._mu:
            self._queue = [r for r in self._queue if r.get("review_id") != review_id]
            cancelled_active = (
                self._active is not None and
                self._active.get("review_id") == review_id
            )
            if cancelled_active:
                self._active = None
            else:
                # Mark so _next() skips it if it arrives late
                self._cancelled_ids.add(review_id)
        if cancelled_active:
            sublime.status_message("SublimeReview: cancelled — file modified by another agent")
            self._next()
        else:
            self._refresh()

    def _next(self):
        with self._mu:
            # Skip over any reviews that were cancelled while queued
            while self._queue and self._queue[0].get("review_id") in self._cancelled_ids:
                self._cancelled_ids.discard(self._queue[0].get("review_id"))
                self._queue.pop(0)
            if not self._queue:
                self._active = None
            else:
                self._active = self._queue.pop(0)
            review = self._active

        if review is None:
            self._inline.clear()
            self._panel.clear()
            self._ind.set_status(0)
            return

        self._inline.clear()
        tool = review.get("tool_name", "")
        if tool in ("Edit", "MultiEdit"):
            ok = self._inline.show(review)
            self._panel.show(review, compact=ok)
        else:
            self._panel.show(review, compact=False)
        self._refresh()

    def _refresh(self):
        with self._mu:
            w = len(self._queue)
            a = self._active is not None
        self._ind.set_status((1 if a else 0) + w, w)

    def accept(self):
        self._decide("allow")

    def reject(self, reason=""):
        self._decide("deny", reason)

    def _decide(self, decision, reason=""):
        with self._mu:
            review = self._active
        if review is None:
            return
        msg = {
            "type": "review_decision",
            "review_id": review.get("review_id"),
            "decision": decision,
        }
        if reason:
            msg["reason"] = reason
        self._send(msg)
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
        if not m:
            return
        self.window.show_input_panel(
            "Rejection reason (optional, Enter to confirm, Escape to skip):",
            "",
            lambda reason: m.reject(reason.strip()),  # Enter
            None,
            lambda: m.reject(""),                     # Escape
        )


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

def plugin_loaded():
    _start_server_if_needed()


# Called by Sublime when the plugin is unloaded (e.g. on plugin reload).
# Closes all WebSocket connections and terminates the review server.
# The server removes hook entries from ~/.claude/settings.json when it
# detects the last client has disconnected.
def plugin_unloaded():
    global _server_proc
    for m in list(_managers.values()):
        m.stop()
    _managers.clear()
    if _server_proc is not None:
        try:
            _server_proc.terminate()
        except Exception:
            pass
        _server_proc = None
