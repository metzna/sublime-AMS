"""
Sublime Review Server — configuration constants.

Pure values: ports, timeouts, paths, hook command templates.  No logic.
Other server modules import what they need from here.

Adjust HTTP/WS ports here (requires server restart).  Timeouts are read
lazily by their consumers and take effect on the next check cycle.
"""

import os

# ─── Ports ───────────────────────────────────────────────────────────────────
HTTP_PORT = 9876       # hook scripts POST review requests here
WS_PORT   = 9877       # Sublime plugin connects via WebSocket here

# ─── Timeouts (seconds) ──────────────────────────────────────────────────────
LOCK_TIMEOUT          = 600   # before an unresolved lock is force-released
REVIEW_TIMEOUT        = 300   # before a pending review is auto-allowed
AGENT_TTL             = 1800  # inactivity before an active agent is pruned
AGENT_FINISHED_TTL    = 30    # before a "finished" agent is pruned
IDLE_SHUTDOWN_SECONDS = 8     # exit after this many seconds with no WS clients

# ─── Paths ───────────────────────────────────────────────────────────────────
LOG_FILE       = os.path.expanduser("~/.claude/sublime_review_server.log")
AUDIT_LOG_PATH = os.path.expanduser("~/.local/share/sublime-agents/audit.jsonl")
SETTINGS_PATH  = os.path.expanduser("~/.claude/settings.json")

# Hooks live one directory up from this file: SublimeReview/hooks/
HOOKS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks")
)

# ─── Hook command templates ──────────────────────────────────────────────────
REVIEW_CMD         = "python3 " + os.path.join(HOOKS_DIR, "sublime_review.py")
ACTIVITY_CMD       = "python3 " + os.path.join(HOOKS_DIR, "activity.py")
POST_TOOL_CMD      = "python3 " + os.path.join(HOOKS_DIR, "post_tool_use.py")
SESSION_START_CMD  = "python3 " + os.path.join(HOOKS_DIR, "session_start.py")
SESSION_END_CMD    = "python3 " + os.path.join(HOOKS_DIR, "sublime_session_end.py")
SUBAGENT_START_CMD = "python3 " + os.path.join(HOOKS_DIR, "subagent_start.py")
SUBAGENT_STOP_CMD  = "python3 " + os.path.join(HOOKS_DIR, "subagent_stop.py")

# Script filenames that belong to this plugin.  Matched by basename so that
# stale entries from a different installation path are also recognised.
OUR_HOOK_BASENAMES = frozenset({
    "sublime_review.py",
    "activity.py",
    "post_tool_use.py",
    "session_start.py",
    "sublime_session_end.py",
    "subagent_start.py",
    "subagent_stop.py",
})
