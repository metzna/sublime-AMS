#!/usr/bin/env python3
"""
Sublime Review Server
=====================
Coordinates Claude Code PreToolUse reviews via two transports:

  HTTP  localhost:9876  — used by hook scripts (sublime_review.py).
                          Each review is a blocking POST that returns only
                          after the user accepts or rejects in Sublime.

  WS    localhost:9877  — used by the Sublime Text plugin.
                          Server pushes review_request messages; plugin
                          pushes back review_decision messages.

State is held entirely in memory and is lost on server restart.  This is
intentional — locks and queued reviews are short-lived by design.
"""

import asyncio
import json
import logging
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread, Lock
from typing import Optional

import websockets

# ─── Configuration ───────────────────────────────────────────────────────────
# Adjust these at the top of the file; no restart needed for LOCK_TIMEOUT
# (it is checked lazily), but HTTP/WS ports require a server restart.

HTTP_PORT = 9876       # hook scripts POST review requests here
WS_PORT = 9877         # Sublime plugin connects via WebSocket here
LOCK_TIMEOUT = 600     # seconds before an unresolved lock is force-released
REVIEW_TIMEOUT = 300   # seconds before a pending review is auto-allowed
LOG_FILE = "~/.claude/sublime_review_server.log"

# ─── State (all guarded by state_lock) ───────────────────────────────────────

state_lock = Lock()

# file_path → lock info.  A lock is held from the moment a review is queued
# until the user accepts/rejects, the session ends, or the lock times out.
# {file_path: {"session_id": str, "agent_label": str, "locked_since": float}}
locks: dict = {}

# review_id → review data + decision slot.
# The hook script's polling loop watches review_data["decision"] until it
# becomes non-None (set by WS message or timeout).
# {review_id: {"decision": str|None, ...review_data}}
pending_reviews: dict = {}

# FIFO queue of review_ids awaiting Sublime
review_queue: list = []

# Connected Sublime WebSocket clients
ws_clients: set = set()

# asyncio event loop (set when WS server starts)
ws_loop: Optional[asyncio.AbstractEventLoop] = None

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("sublime_review_server")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def agent_label(session_id: str) -> str:
    """Derive a short human-readable label from a session_id."""
    return f"Agent-{session_id[:6]}"


def broadcast_ws(message: dict) -> None:
    """Schedule a WS broadcast from any thread."""
    if ws_loop is None:
        return
    asyncio.run_coroutine_threadsafe(_broadcast(json.dumps(message)), ws_loop)


async def _broadcast(data: str) -> None:
    dead = set()
    for ws in list(ws_clients):
        try:
            await ws.send(data)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


def push_lock_update() -> None:
    """Broadcast current lock state to all Sublime clients."""
    with state_lock:
        snapshot = {
            fp: {"agent_label": info["agent_label"], "since": info["locked_since"]}
            for fp, info in locks.items()
        }
    broadcast_ws({"type": "lock_update", "locks": snapshot})


def expire_locks() -> None:
    """Remove locks older than LOCK_TIMEOUT. Called periodically."""
    now = time.time()
    expired = []
    with state_lock:
        for fp, info in list(locks.items()):
            if now - info["locked_since"] > LOCK_TIMEOUT:
                expired.append(fp)
                del locks[fp]
    if expired:
        log.info("Expired locks: %s", expired)
        push_lock_update()


def release_lock(file_path: str) -> None:
    with state_lock:
        locks.pop(file_path, None)
    push_lock_update()


def release_session_locks(session_id: str) -> list:
    released = []
    with state_lock:
        for fp in list(locks.keys()):
            if locks[fp]["session_id"] == session_id:
                del locks[fp]
                released.append(fp)
    if released:
        log.info("Released locks for session %s: %s", session_id, released)
        push_lock_update()
    return released


# ─── HTTP Request Handler ─────────────────────────────────────────────────────

class ReviewHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("HTTP %s", fmt % args)

    def send_json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except BrokenPipeError:
            log.warning("BrokenPipe sending response — hook process likely died before we responded")

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw)

    def do_POST(self):
        if self.path == "/review":
            self._handle_review()
        elif self.path == "/unlock_session":
            self._handle_unlock_session()
        elif self.path == "/unlock_file":
            self._handle_unlock_file()
        elif self.path == "/status":
            self._handle_status()
        else:
            self.send_json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/status":
            self._handle_status()
        else:
            self.send_json(404, {"error": "not found"})

    # ── /review ───────────────────────────────────────────────────────────────

    def _handle_review(self):
        try:
            body = self.read_body()
        except Exception as e:
            self.send_json(400, {"error": f"invalid JSON: {e}"})
            return

        session_id = body.get("session_id", "unknown")
        file_path = body.get("file_path", "")
        tool_name = body.get("tool_name", "")

        # Check / acquire file lock
        with state_lock:
            existing = locks.get(file_path)
            if existing and existing["session_id"] != session_id:
                owner = existing["agent_label"]
                log.info("Lock conflict: %s locked by %s, denied for %s", file_path, owner, session_id)
                self.send_json(200, {
                    "decision": "deny",
                    "reason": f"file locked by {owner}",
                })
                return

            label = agent_label(session_id)
            locks[file_path] = {
                "session_id": session_id,
                "agent_label": label,
                "locked_since": time.time(),
            }
            review_id = str(uuid.uuid4())
            review_data = {
                **body,
                "review_id": review_id,
                "agent_label": label,
                "queued_at": time.time(),
                "decision": None,
            }
            pending_reviews[review_id] = review_data
            review_queue.append(review_id)
            queue_total = len(review_queue)
            queue_position = queue_total

        push_lock_update()

        # Notify Sublime
        ws_message = {
            "type": "review_request",
            "review_id": review_id,
            "session_id": session_id,
            "agent_label": label,
            "tool_name": tool_name,
            "file_path": file_path,
            "old_string": body.get("old_string", ""),
            "new_string": body.get("new_string", ""),
            "content": body.get("content", ""),
            "cwd": body.get("cwd", ""),
            "queue_position": queue_position,
            "queue_total": queue_total,
        }
        broadcast_ws(ws_message)
        log.info("Review queued: %s  file=%s  session=%s", review_id, file_path, session_id)

        # Block until decision or timeout
        deadline = time.time() + REVIEW_TIMEOUT
        while True:
            time.sleep(0.2)
            with state_lock:
                decision = pending_reviews.get(review_id, {}).get("decision")
            if decision is not None:
                break
            if time.time() > deadline:
                decision = "allow"   # timeout → auto-allow
                log.info("Review %s timed out, auto-allowing", review_id)
                break

        # Cleanup — and invalidate any queued reviews for the same file
        # from other sessions so they get a clean deny instead of a stale edit error
        invalidated = []
        with state_lock:
            pending_reviews.pop(review_id, None)
            if review_id in review_queue:
                review_queue.remove(review_id)
            locks.pop(file_path, None)

            if decision == "allow":
                for rid in list(review_queue):
                    r = pending_reviews.get(rid)
                    if r and r.get("file_path") == file_path and r.get("session_id") != session_id:
                        r["decision"] = "deny"
                        r["reason"] = "file was modified by another agent while queued"
                        invalidated.append(rid)

        if invalidated:
            log.info("Auto-denied %d stale review(s) for %s after accept", len(invalidated), file_path)
            for rid in invalidated:
                broadcast_ws({"type": "review_cancelled", "review_id": rid,
                              "reason": "file was modified by another agent"})

        push_lock_update()
        _broadcast_queue_positions()

        log.info("Review %s decision=%s", review_id, decision)
        self.send_json(200, {"decision": decision})

    # ── /unlock_session ───────────────────────────────────────────────────────

    def _handle_unlock_session(self):
        try:
            body = self.read_body()
        except Exception as e:
            self.send_json(400, {"error": str(e)})
            return
        session_id = body.get("session_id", "")
        released = release_session_locks(session_id)
        self.send_json(200, {"released": released})

    # ── /unlock_file ──────────────────────────────────────────────────────────

    def _handle_unlock_file(self):
        try:
            body = self.read_body()
        except Exception as e:
            self.send_json(400, {"error": str(e)})
            return
        file_path = body.get("file_path", "")
        release_lock(file_path)
        self.send_json(200, {"released": file_path})

    # ── /status ───────────────────────────────────────────────────────────────

    def _handle_status(self):
        with state_lock:
            self.send_json(200, {
                "locks": locks,
                "queue": review_queue,
                "pending_reviews": len(pending_reviews),
                "ws_clients": len(ws_clients),
            })


def _broadcast_queue_positions() -> None:
    """Tell Sublime about updated queue sizes after a review resolves."""
    with state_lock:
        total = len(review_queue)
    broadcast_ws({"type": "queue_update", "queue_total": total})


# ─── WebSocket Server ─────────────────────────────────────────────────────────

async def ws_handler(websocket) -> None:
    ws_clients.add(websocket)
    log.info("Sublime connected (total clients: %d)", len(ws_clients))

    # Send current state immediately
    with state_lock:
        snapshot = {
            fp: {"agent_label": info["agent_label"], "since": info["locked_since"]}
            for fp, info in locks.items()
        }
    await websocket.send(json.dumps({"type": "lock_update", "locks": snapshot}))

    # Resend any reviews still waiting for a decision
    with state_lock:
        pending = [
            dict(r, queue_position=i+1, queue_total=len(review_queue))
            for i, rid in enumerate(review_queue)
            if rid in pending_reviews
            for r in [pending_reviews[rid]]
        ]
    for review in pending:
        msg = {
            "type": "review_request",
            "review_id": review.get("review_id"),
            "session_id": review.get("session_id"),
            "agent_label": review.get("agent_label"),
            "tool_name": review.get("tool_name"),
            "file_path": review.get("file_path"),
            "old_string": review.get("old_string", ""),
            "new_string": review.get("new_string", ""),
            "content": review.get("content", ""),
            "cwd": review.get("cwd", ""),
            "queue_position": review.get("queue_position", 1),
            "queue_total": review.get("queue_total", 1),
        }
        await websocket.send(json.dumps(msg))
        log.info("Resent pending review %s to reconnected client", review.get("review_id"))

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except Exception:
                log.warning("WS: invalid JSON: %s", raw)
                continue

            msg_type = msg.get("type")

            if msg_type == "review_decision":
                review_id = msg.get("review_id")
                decision = msg.get("decision")
                log.info("WS decision: review=%s decision=%s", review_id, decision)
                with state_lock:
                    if review_id in pending_reviews:
                        pending_reviews[review_id]["decision"] = decision

            elif msg_type == "unlock_file":
                file_path = msg.get("file_path", "")
                release_lock(file_path)
                log.info("WS manual unlock: %s", file_path)

            elif msg_type == "ping":
                await websocket.send(json.dumps({"type": "pong"}))

            else:
                log.warning("WS: unknown message type: %s", msg_type)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        ws_clients.discard(websocket)
        log.info("Sublime disconnected (total clients: %d)", len(ws_clients))


async def run_ws_server() -> None:
    global ws_loop
    ws_loop = asyncio.get_event_loop()
    async with websockets.serve(ws_handler, "localhost", WS_PORT):
        log.info("WebSocket server listening on ws://localhost:%d", WS_PORT)
        await asyncio.Future()   # run forever


# ─── Lock Expiry Watchdog ─────────────────────────────────────────────────────

def lock_expiry_watchdog() -> None:
    while True:
        time.sleep(60)
        expire_locks()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Starting Sublime Review Server")

    # WebSocket in its own thread with its own event loop
    def _ws_thread():
        asyncio.run(run_ws_server())

    ws_t = Thread(target=_ws_thread, daemon=True)
    ws_t.start()

    # Lock expiry watchdog
    wd_t = Thread(target=lock_expiry_watchdog, daemon=True)
    wd_t.start()

    # HTTP server (blocking, main thread)
    server = HTTPServer(("localhost", HTTP_PORT), ReviewHandler)
    log.info("HTTP server listening on http://localhost:%d", HTTP_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server stopped by user")


if __name__ == "__main__":
    main()
