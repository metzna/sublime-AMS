#!/usr/bin/env python3
"""
PreToolUse Hook: sublime_review.py
===================================
Called by Claude Code before every Edit, Write, or MultiEdit tool call.
Claude Code blocks and waits for this script to exit before proceeding.

Flow:
  1. Read the tool call JSON from stdin (provided by Claude Code).
  2. Extract the file path and proposed change.
  3. POST to the review server, which blocks until the user decides.
  4. Exit 0  → allow  (Claude writes the file).
     Exit 2  → deny   (Claude receives the stderr message and may retry).

Fail-open behaviour:
  If the review server is not reachable, this script exits 0 (allow) so
  Claude is never permanently blocked by a missing server.  A warning is
  printed to stderr (visible in the Claude Code session log).

Configure in .claude/settings.json:
  {
    "hooks": {
      "PreToolUse": [{
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [{"type": "command",
                   "command": "python3 ~/.claude/hooks/sublime_review.py",
                   "timeout": 300}]
      }]
    }
  }

The timeout (300 s = 5 min) is how long Claude waits for a decision.
The server auto-allows the review after the same interval so the hook
script always gets a response before the timeout fires.
"""

import json
import sys
import os
import urllib.request
import urllib.error

SERVER_URL = "http://localhost:9876/review"

# Slightly longer than the server's REVIEW_TIMEOUT so the server always
# responds before the OS-level socket timeout fires.
TIMEOUT = 310


def main() -> None:
    # ── Read hook payload from stdin ──────────────────────────────────────────
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw)
    except Exception as e:
        print("sublime_review: failed to parse stdin: {}".format(e), file=sys.stderr)
        sys.exit(0)

    tool_name = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input", {})
    session_id = hook_data.get("session_id", "unknown")
    cwd = os.environ.get("CLAUDE_CWD", os.getcwd())

    # ── Resolve the target file path ──────────────────────────────────────────
    # Claude may pass a relative path; normalise to absolute so the server
    # and Sublime plugin always receive a canonical path.
    file_path = tool_input.get("path") or tool_input.get("file_path", "")
    if not file_path:
        sys.exit(0)   # no file involved — nothing to review

    if not os.path.isabs(file_path):
        file_path = os.path.join(cwd, file_path)

    # ── Skip review for files under ~/.claude/ ────────────────────────────────
    # Memory, plans, settings, and other Claude-internal state live here.
    # The user does not want to approve every memory write or plan update.
    claude_dir = os.path.expanduser("~/.claude")
    if file_path == claude_dir or file_path.startswith(claude_dir + os.sep):
        sys.exit(0)

    # ── Build the review payload ───────────────────────────────────────────────
    payload: dict = {
        "session_id": session_id,
        "tool_name": tool_name,
        "file_path": file_path,
        "cwd": cwd,
    }

    if tool_name == "Edit":
        # Targeted replacement: show old vs new string as a diff.
        payload["old_string"] = tool_input.get("old_string", "")
        payload["new_string"] = tool_input.get("new_string", "")

    elif tool_name == "Write":
        # Full file write: show entire new content as additions.
        payload["content"] = tool_input.get("content", "")

    elif tool_name == "MultiEdit":
        # Multiple replacements in one call: concatenate for display.
        edits = tool_input.get("edits", [])
        payload["old_string"] = "\n---\n".join(e.get("old_string", "") for e in edits)
        payload["new_string"] = "\n---\n".join(e.get("new_string", "") for e in edits)
        payload["edits"] = edits

    else:
        # Unknown tool — pass through without review.
        sys.exit(0)

    # ── Send to review server (blocking) ──────────────────────────────────────
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            SERVER_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result = json.loads(resp.read())

    except urllib.error.URLError as e:
        # Server not reachable — fail open so Claude is not blocked.
        print(
            "sublime_review: server unreachable ({}), allowing by default".format(e),
            file=sys.stderr,
        )
        sys.exit(0)

    except Exception as e:
        print(
            "sublime_review: unexpected error ({}), allowing by default".format(e),
            file=sys.stderr,
        )
        sys.exit(0)

    # ── Act on the decision ───────────────────────────────────────────────────
    decision = result.get("decision", "allow")
    reason   = result.get("reason", "")

    if decision == "allow":
        sys.exit(0)
    else:
        msg = "Change to {} was rejected by the reviewer.".format(file_path)
        if reason:
            msg += " Reason: {}".format(reason)
        print(msg, file=sys.stderr)
        sys.exit(2)   # exit 2 = Claude Code reads stderr and shows it to Claude


if __name__ == "__main__":
    main()
