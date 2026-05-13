"""
SublimeReview – main plugin logic.

Manages the WebSocket connection to the review server, the review queue,
keybinding commands, and coordinates the panel + lock indicator.
"""

import threading
import json
from typing import Optional

import sublime
import sublime_plugin

from . import settings as cfg
from .review_panel import ReviewPanel
from .lock_indicator import LockIndicator
from .ws_client import WebSocketClient


# ─── Global plugin state ──────────────────────────────────────────────────────

_instances: dict = {}   # window_id → ReviewManager


def _get_manager(window: Optional[sublime.Window] = None) -> Optional["ReviewManager"]:
    if window is None:
        window = sublime.active_window()
    if window is None:
        return None
    wid = window.id()
    if wid not in _instances:
        mgr = ReviewManager(window)
        _instances[wid] = mgr
        mgr.start()
    return _instances[wid]


# ─── ReviewManager ────────────────────────────────────────────────────────────

class ReviewManager:
    """
    Per-window coordinator. Owns:
      - WebSocket connection thread
      - Review queue (FIFO)
      - Active review state
      - ReviewPanel and LockIndicator
    """

    def __init__(self, window: sublime.Window) -> None:
        self._window = window
        self._panel = ReviewPanel(window)
        self._indicator = LockIndicator(window)

        self._queue = []          # list of review dicts
        self._active_review = None
        self._lock = threading.Lock()

        self._ws: Optional[WebSocketClient] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._connect()

    def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._indicator.clear_all()
        self._indicator.clear_status()
        self._panel.clear()

    # ── WebSocket ─────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        host = cfg.server_host()
        port = cfg.server_port()
        url = f"ws://{host}:{port}"

        self._ws = WebSocketClient(
            url,
            on_message=self._on_message,
            on_open=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
        )

        def _try_connect():
            try:
                self._ws.connect()
            except Exception as e:
                sublime.set_timeout(
                    lambda: sublime.status_message(f"SublimeReview: connection failed — {e}"), 0
                )
                if self._running and cfg.auto_reconnect():
                    delay = cfg.reconnect_delay() * 1000
                    sublime.set_timeout_async(self._reconnect, delay)

        threading.Thread(target=_try_connect, daemon=True).start()

    def _on_open(self) -> None:
        sublime.set_timeout(
            lambda: sublime.status_message("SublimeReview: connected to review server"), 0
        )

    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return

        msg_type = msg.get("type")

        if msg_type == "review_request":
            sublime.set_timeout(lambda: self._enqueue_review(msg), 0)

        elif msg_type == "lock_update":
            locks = msg.get("locks", {})
            sublime.set_timeout(lambda: self._apply_locks(locks), 0)

        elif msg_type == "queue_update":
            total = msg.get("queue_total", 0)
            sublime.set_timeout(lambda: self._update_status_bar(total), 0)

    def _on_error(self, error: Exception) -> None:
        sublime.set_timeout(
            lambda: sublime.status_message(f"SublimeReview: WS error — {error}"), 0
        )

    def _on_close(self) -> None:
        if not self._running:
            return
        if cfg.auto_reconnect():
            delay = cfg.reconnect_delay() * 1000
            sublime.set_timeout_async(self._reconnect, delay)

    def _reconnect(self) -> None:
        if not self._running:
            return
        self._connect()

    # ── Review queue ──────────────────────────────────────────────────────────

    def _enqueue_review(self, review: dict) -> None:
        with self._lock:
            self._queue.append(review)
            if self._active_review is None:
                self._show_next_review()
            else:
                self._refresh_status()

    def _show_next_review(self) -> None:
        """Show the next review from the queue. Must be called from the main thread."""
        with self._lock:
            if not self._queue:
                self._active_review = None
                self._panel.clear()
                self._indicator.clear_status()
                return
            self._active_review = self._queue.pop(0)

        review = self._active_review
        file_path = review.get("file_path", "")

        if file_path:
            self._window.open_file(file_path, sublime.TRANSIENT)

        self._panel.show(review)
        self._refresh_status()

    def _refresh_status(self) -> None:
        with self._lock:
            waiting = len(self._queue)
            pending = (1 if self._active_review else 0) + waiting
        self._indicator.update_status(pending, waiting)

    def _update_status_bar(self, queue_total: int) -> None:
        self._indicator.update_status(queue_total)

    def _apply_locks(self, locks: dict) -> None:
        self._indicator.apply_locks(locks)

    # ── Decisions ─────────────────────────────────────────────────────────────

    def accept(self) -> None:
        self._decide("allow")

    def reject(self) -> None:
        self._decide("deny")

    def _decide(self, decision: str) -> None:
        with self._lock:
            review = self._active_review
        if review is None:
            return

        review_id = review.get("review_id")
        self._send({"type": "review_decision", "review_id": review_id, "decision": decision})
        sublime.set_timeout(self._show_next_review, 0)

    def next_review(self) -> None:
        """Cycle to the next queued review without deciding (Tab key)."""
        with self._lock:
            if not self._queue:
                return
            if self._active_review:
                self._queue.append(self._active_review)
            self._active_review = None
        self._show_next_review()

    def manual_unlock(self, file_path: str) -> None:
        self._send({"type": "unlock_file", "file_path": file_path})

    # ── WebSocket send ────────────────────────────────────────────────────────

    def _send(self, msg: dict) -> None:
        if self._ws:
            try:
                self._ws.send(json.dumps(msg))
            except Exception as e:
                sublime.set_timeout(
                    lambda: sublime.status_message(f"SublimeReview: send error — {e}"), 0
                )


# ─── Sublime Commands ─────────────────────────────────────────────────────────

class SublimeReviewAcceptCommand(sublime_plugin.WindowCommand):
    def run(self):
        mgr = _get_manager(self.window)
        if mgr:
            mgr.accept()


class SublimeReviewRejectCommand(sublime_plugin.WindowCommand):
    def run(self):
        mgr = _get_manager(self.window)
        if mgr:
            mgr.reject()


class SublimeReviewNextCommand(sublime_plugin.WindowCommand):
    def run(self):
        mgr = _get_manager(self.window)
        if mgr:
            mgr.next_review()


class SublimeReviewUnlockFileCommand(sublime_plugin.WindowCommand):
    """Command palette: manually unlock a file."""
    def run(self, file_path=""):
        if not file_path:
            view = self.window.active_view()
            file_path = view.file_name() if view else ""
        if not file_path:
            sublime.status_message("SublimeReview: no file selected")
            return
        mgr = _get_manager(self.window)
        if mgr:
            mgr.manual_unlock(file_path)
            sublime.status_message(f"SublimeReview: unlocked {file_path}")


class SublimeReviewConnectCommand(sublime_plugin.WindowCommand):
    """Manually (re)connect to the review server."""
    def run(self):
        mgr = _get_manager(self.window)
        if mgr:
            mgr.stop()
            mgr.start()
            sublime.status_message("SublimeReview: reconnecting…")


# ─── Plugin lifecycle ─────────────────────────────────────────────────────────

class SublimeReviewListener(sublime_plugin.EventListener):
    def on_activated(self, view: sublime.View) -> None:
        window = view.window()
        if window:
            _get_manager(window)

    def on_pre_close_window(self, window: sublime.Window) -> None:
        wid = window.id()
        mgr = _instances.pop(wid, None)
        if mgr:
            mgr.stop()
