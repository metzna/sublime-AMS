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

This module is launched as a standalone script by the plugin.  It adds its
own directory to sys.path so the sibling modules `config` and `hookmanager`
import cleanly without requiring the parent package to be importable.
"""

import os
import sys

# Make sibling modules importable when launched as a script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import json
import logging
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread, Lock, Event, Timer
from typing import Optional

import websockets

import config
from hookmanager import enable_hooks, disable_hooks


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

# session_id → agent info.  Populated on first review from each session;
# status set to "finished" on SessionEnd.
agents: dict = {}

# Connected Sublime WebSocket clients
ws_clients: set = set()

# asyncio event loop (set when WS server starts)
ws_loop: Optional[asyncio.AbstractEventLoop] = None

# Audit log write lock (separate from state_lock to avoid contention)
_audit_lock = Lock()


# ─── Logging ─────────────────────────────────────────────────────────────────

try:
    os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)
    _file_handler = logging.FileHandler(config.LOG_FILE)
except Exception:
    _file_handler = None

_handlers = [logging.StreamHandler()]
if _file_handler is not None:
    _handlers.append(_file_handler)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("server")


# ─── Audit log ───────────────────────────────────────────────────────────────

def _audit(event_type: str, **kwargs) -> None:
    """Append one event line to the append-only audit JSONL (best-effort)."""
    try:
        os.makedirs(os.path.dirname(config.AUDIT_LOG_PATH), exist_ok=True)
        record = json.dumps({"ts": time.time(), "event": event_type, **kwargs})
        with _audit_lock:
            with open(config.AUDIT_LOG_PATH, "a") as f:
                f.write(record + "\n")
    except Exception:
        pass


# ─── Helpers ─────────────────────────────────────────────────────────────────

def agent_label(session_id: str) -> str:
    """Derive a short human-readable label from a session_id."""
    return f"Agent-{session_id[:6]}"


def broadcast_ws(message: dict) -> None:
    """Schedule a WS broadcast from any thread."""
    if ws_loop is None:
        return
    msg_type = message.get("type", "?")
    if msg_type == "agent_update":
        sids = sorted(message.get("agents", {}).keys())
        log.info("broadcast agent_update -> %d client(s): [%s]",
                 len(ws_clients), ", ".join(s[:6] for s in sids))
    elif msg_type == "lock_update":
        log.info("broadcast lock_update -> %d client(s): %d lock(s)",
                 len(ws_clients), len(message.get("locks", {})))
    else:
        log.info("broadcast %s -> %d client(s)", msg_type, len(ws_clients))
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


def _agent_snapshot() -> dict:
    """Build agent snapshot dict. Must be called with state_lock held."""
    lock_counts: dict = {}
    for fp, info in locks.items():
        sid = info["session_id"]
        lock_counts[sid] = lock_counts.get(sid, 0) + 1
    # Map session_id -> file being reviewed (for agents blocked on review)
    reviewing: dict = {}
    for rev in pending_reviews.values():
        sid = rev.get("session_id", "")
        fp  = rev.get("file_path", "")
        if sid and rev.get("decision") is None:
            reviewing[sid] = fp
    return {
        sid: {
            "type":              a.get("type", "claude_code"),
            "status":            a.get("status", "active"),
            "cwd":               a.get("cwd", ""),
            "last_action":       a.get("last_action", ""),
            "last_seen":         a.get("last_seen", 0.0),
            "lock_count":        lock_counts.get(sid, 0),
            "awaiting_review":   reviewing.get(sid),
            "running_subagents": len(a.get("running_subagents", [])),
        }
        for sid, a in agents.items()
    }


def push_agent_update() -> None:
    """Broadcast current agent registry to all Sublime clients."""
    with state_lock:
        snapshot = _agent_snapshot()
    broadcast_ws({"type": "agent_update", "agents": snapshot})


def expire_locks() -> None:
    """Remove locks older than LOCK_TIMEOUT. Called periodically."""
    now = time.time()
    expired = []
    with state_lock:
        for fp, info in list(locks.items()):
            if now - info["locked_since"] > config.LOCK_TIMEOUT:
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
        elif self.path == "/activity":
            self._handle_activity()
        elif self.path == "/subagent/start":
            self._handle_subagent_start()
        elif self.path == "/subagent/stop":
            self._handle_subagent_stop()
        elif self.path == "/unlock_session":
            self._handle_unlock_session()
        elif self.path == "/unlock_file":
            self._handle_unlock_file()
        elif self.path == "/status":
            self._handle_status()
        elif self.path == "/shutdown":
            self._handle_shutdown()
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

            # Derive a human-readable action for the dashboard
            bn = os.path.basename(file_path)
            if tool_name in ("Edit", "MultiEdit"):
                last_action = "editing " + bn
            elif tool_name == "Write":
                last_action = "writing " + bn
            else:
                last_action = tool_name
            existing = agents.get(session_id, {})
            agents[session_id] = {
                "type":              body.get("agent_type", "claude_code"),
                "status":            "active",
                "cwd":               body.get("cwd", "") or existing.get("cwd", ""),
                "last_action":       last_action,
                "last_seen":         time.time(),
                "parent_session_id": existing.get("parent_session_id"),
                "running_subagents": existing.get("running_subagents", []),
            }

            review_id = str(uuid.uuid4())
            _ev = Event()
            review_data = {
                **body,
                "review_id": review_id,
                "agent_label": label,
                "queued_at": time.time(),
                "decision": None,
                "_event": _ev,
            }
            pending_reviews[review_id] = review_data
            review_queue.append(review_id)
            queue_total = len(review_queue)
            queue_position = queue_total

        push_lock_update()
        push_agent_update()
        _audit("review_queued", session_id=session_id, review_id=review_id,
               tool_name=tool_name, file_path=file_path)

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
        _ev.wait(timeout=config.REVIEW_TIMEOUT)
        with state_lock:
            _rev = pending_reviews.get(review_id, {})
            decision = _rev.get("decision")
            reason   = _rev.get("reason", "")
        if decision is None:
            decision = "allow"
            reason   = ""
            log.info("Review %s timed out, auto-allowing", review_id)

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
                        ev = r.get("_event")
                        if ev:
                            ev.set()
                        invalidated.append(rid)

        if invalidated:
            log.info("Auto-denied %d stale review(s) for %s after accept", len(invalidated), file_path)
            for rid in invalidated:
                broadcast_ws({"type": "review_cancelled", "review_id": rid,
                              "reason": "file was modified by another agent"})

        push_lock_update()
        _broadcast_queue_positions()

        _audit("review_decision", session_id=session_id, review_id=review_id,
               decision=decision, reason=reason)
        log.info("Review %s decision=%s", review_id, decision)
        self.send_json(200, {"decision": decision, "reason": reason})

    # ── /activity ─────────────────────────────────────────────────────────────

    def _handle_activity(self):
        try:
            body = self.read_body()
        except Exception as e:
            self.send_json(400, {"error": str(e)})
            return
        session_id  = body.get("session_id", "unknown")
        event_type  = body.get("event_type", "pre_tool_use")
        cwd         = body.get("cwd", "")

        if event_type == "post_tool_use":
            tool_name      = body.get("tool_name", "")
            result_summary = body.get("result_summary")
            result_snippet = body.get("result_snippet", "")
            _audit("post_tool_use", session_id=session_id,
                   tool_name=tool_name, result_summary=result_summary,
                   result_snippet=result_snippet)
            with state_lock:
                if session_id in agents:
                    agents[session_id]["last_seen"] = time.time()
                    if result_summary:
                        agents[session_id]["last_action"] = result_summary
            if result_summary:
                push_agent_update()
        elif event_type == "session_start":
            source = body.get("source", "startup")
            action = "session resumed" if source == "resume" else "session started"
            with state_lock:
                existing = agents.get(session_id, {})
                agents[session_id] = {
                    "type":              existing.get("type", "claude_code"),
                    "status":            "active",
                    "cwd":               cwd or existing.get("cwd", ""),
                    "last_action":       action,
                    "last_seen":         time.time(),
                    "parent_session_id": existing.get("parent_session_id"),
                    "running_subagents": existing.get("running_subagents", []),
                }
            _audit("session_start", session_id=session_id, source=source)
            push_agent_update()
        else:
            # PreToolUse: update dashboard action + write audit
            action = body.get("action", "")
            with state_lock:
                existing = agents.get(session_id, {})
                agents[session_id] = {
                    "type":              existing.get("type", "claude_code"),
                    "status":            "active",
                    "cwd":               cwd or existing.get("cwd", ""),
                    "last_action":       action,
                    "last_seen":         time.time(),
                    "parent_session_id": existing.get("parent_session_id"),
                    "running_subagents": existing.get("running_subagents", []),
                }
            _audit("activity", session_id=session_id, action=action)
            push_agent_update()

        self.send_json(200, {"ok": True})

    # ── /subagent/start ───────────────────────────────────────────────────────

    def _handle_subagent_start(self):
        # Subagents share the parent's session_id — no separate registry entry.
        # We mark the parent as currently delegating to a subagent.
        try:
            body = self.read_body()
        except Exception as e:
            self.send_json(400, {"error": str(e)})
            return
        parent_sid = body.get("parent_session_id", "unknown")
        agent_id   = body.get("agent_id", "")
        agent_type = body.get("agent_type", "subagent")
        if not agent_id:
            self.send_json(400, {"error": "agent_id required"})
            return
        with state_lock:
            if parent_sid not in agents:
                agents[parent_sid] = {
                    "type": "claude_code", "status": "active",
                    "cwd": body.get("cwd", ""), "last_action": "spawning subagent",
                    "last_seen": time.time(), "parent_session_id": None,
                    "children": [], "running_subagents": [],
                }
            subagents = agents[parent_sid].setdefault("running_subagents", [])
            if agent_id not in subagents:
                subagents.append(agent_id)
        log.info("Subagent started: %s (parent: %s)", agent_id, parent_sid)
        _audit("subagent_started", agent_id=agent_id, parent_session_id=parent_sid,
               agent_type=agent_type)
        push_agent_update()
        self.send_json(200, {"ok": True})

    # ── /subagent/stop ────────────────────────────────────────────────────────

    def _handle_subagent_stop(self):
        try:
            body = self.read_body()
        except Exception as e:
            self.send_json(400, {"error": str(e)})
            return
        parent_sid = body.get("parent_session_id", "unknown")
        agent_id   = body.get("agent_id", "")
        if not agent_id:
            self.send_json(400, {"error": "agent_id required"})
            return
        with state_lock:
            if parent_sid in agents:
                subagents = agents[parent_sid].get("running_subagents", [])
                if agent_id in subagents:
                    subagents.remove(agent_id)
        log.info("Subagent finished: %s", agent_id)
        _audit("subagent_finished", agent_id=agent_id, parent_session_id=parent_sid)
        push_agent_update()
        self.send_json(200, {"ok": True})

    # ── /unlock_session ───────────────────────────────────────────────────────

    def _handle_unlock_session(self):
        try:
            body = self.read_body()
        except Exception as e:
            self.send_json(400, {"error": str(e)})
            return
        session_id = body.get("session_id", "")
        released = release_session_locks(session_id)
        with state_lock:
            if session_id in agents:
                agents[session_id]["status"] = "finished"
                agents[session_id]["last_seen"] = time.time()
                agents[session_id]["last_action"] = "session ended"
                agents[session_id]["running_subagents"] = []
        _audit("session_ended", session_id=session_id, released_locks=released)
        push_agent_update()
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
                "agents": _agent_snapshot(),
            })

    # ── /shutdown ─────────────────────────────────────────────────────────────

    def _handle_shutdown(self):
        """Exit the server. Called by the plugin's atexit handler on Sublime quit."""
        self.send_json(200, {"ok": True})
        log.info("Received /shutdown — exiting")
        # Exit from a background thread so the HTTP response flushes first
        Timer(0.1, lambda: os._exit(0)).start()


def _broadcast_queue_positions() -> None:
    """Tell Sublime about updated queue sizes after a review resolves."""
    with state_lock:
        total = len(review_queue)
    broadcast_ws({"type": "queue_update", "queue_total": total})


# ─── Idle shutdown ────────────────────────────────────────────────────────────
# When the last Sublime client disconnects, the server schedules itself to exit.
# This lets state survive a plugin reload (WS reconnects within ~3 s) while
# guaranteeing a fresh process — and therefore a fresh agents registry — on
# Sublime restart (no client reconnects within the grace period).
#
# We only arm the shutdown once at least one client has connected, so the
# server doesn't die before the first Sublime ever attaches.

_idle_shutdown_timer = None
_idle_shutdown_lock = Lock()
_had_a_client = False


def _schedule_idle_shutdown() -> None:
    global _idle_shutdown_timer
    with _idle_shutdown_lock:
        if _idle_shutdown_timer is not None:
            _idle_shutdown_timer.cancel()
        def _shutdown():
            if len(ws_clients) == 0:
                log.info("No WS clients for %d s — exiting", config.IDLE_SHUTDOWN_SECONDS)
                os._exit(0)
        _idle_shutdown_timer = Timer(config.IDLE_SHUTDOWN_SECONDS, _shutdown)
        _idle_shutdown_timer.daemon = True
        _idle_shutdown_timer.start()


def _cancel_idle_shutdown() -> None:
    global _idle_shutdown_timer
    with _idle_shutdown_lock:
        if _idle_shutdown_timer is not None:
            _idle_shutdown_timer.cancel()
            _idle_shutdown_timer = None


# ─── WebSocket Server ─────────────────────────────────────────────────────────

async def ws_handler(websocket) -> None:
    global _had_a_client
    ws_clients.add(websocket)
    _had_a_client = True
    _cancel_idle_shutdown()
    log.info("Sublime connected (total clients: %d)", len(ws_clients))
    if len(ws_clients) == 1:
        enable_hooks()

    # Send current state immediately
    with state_lock:
        lock_snapshot = {
            fp: {"agent_label": info["agent_label"], "since": info["locked_since"]}
            for fp, info in locks.items()
        }
        agent_snap = _agent_snapshot()
    await websocket.send(json.dumps({"type": "lock_update", "locks": lock_snapshot}))
    await websocket.send(json.dumps({"type": "agent_update", "agents": agent_snap}))

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
                reason = msg.get("reason", "")
                log.info("WS decision: review=%s decision=%s", review_id, decision)
                ev = None
                with state_lock:
                    if review_id in pending_reviews:
                        pending_reviews[review_id]["decision"] = decision
                        if reason:
                            pending_reviews[review_id]["reason"] = reason
                        ev = pending_reviews[review_id].get("_event")
                if ev is not None:
                    ev.set()

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
        if len(ws_clients) == 0:
            disable_hooks()
            if _had_a_client:
                _schedule_idle_shutdown()


async def run_ws_server() -> None:
    global ws_loop
    ws_loop = asyncio.get_event_loop()
    async with websockets.serve(ws_handler, "localhost", config.WS_PORT):
        log.info("WebSocket server listening on ws://localhost:%d", config.WS_PORT)
        await asyncio.Future()   # run forever


# ─── Agent Expiry ─────────────────────────────────────────────────────────────

def expire_agents() -> None:
    """Remove stale agents: finished ones after AGENT_FINISHED_TTL, active after AGENT_TTL."""
    now = time.time()
    pruned = []
    with state_lock:
        for sid in list(agents):
            a = agents[sid]
            ttl = config.AGENT_FINISHED_TTL if a.get("status") == "finished" else config.AGENT_TTL
            if now - a.get("last_seen", 0) > ttl:
                pruned.append(sid)
                del agents[sid]
    if pruned:
        log.info("Pruned stale agents: %s", pruned)
        push_agent_update()


# ─── Lock Expiry Watchdog ─────────────────────────────────────────────────────

def lock_expiry_watchdog() -> None:
    while True:
        time.sleep(60)
        expire_locks()
        expire_agents()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Starting Sublime Review Server")
    disable_hooks()  # clean up any hooks left over from a previous crash

    # WebSocket in its own thread with its own event loop
    def _ws_thread():
        asyncio.run(run_ws_server())

    ws_t = Thread(target=_ws_thread, daemon=True)
    ws_t.start()

    # Lock expiry watchdog
    wd_t = Thread(target=lock_expiry_watchdog, daemon=True)
    wd_t.start()

    # HTTP server (blocking, main thread)
    server = ThreadingHTTPServer(("localhost", config.HTTP_PORT), ReviewHandler)
    log.info("HTTP server listening on http://localhost:%d", config.HTTP_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server stopped by user")


if __name__ == "__main__":
    main()
