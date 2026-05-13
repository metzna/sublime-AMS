#!/usr/bin/env python3
"""
PreToolUse Hook: sublime_review.py
Intercepts Edit, Write, and MultiEdit tool calls and sends them to the
Sublime Review Server for human review before Claude writes the file.

Exit codes:
  0 → allow (write the file)
  2 → deny  (block; stderr message is returned to Claude)
"""

import json
import sys
import os
import urllib.request
import urllib.error

SERVER_URL = "http://localhost:9876/review"
TIMEOUT = 310       # slightly above server REVIEW_TIMEOUT so server always answers first


def main() -> None:
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw)
    except Exception as e:
        print(f"sublime_review: failed to parse stdin: {e}", file=sys.stderr)
        sys.exit(0)     # don't block Claude if we can't parse

    tool_name = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input", {})
    session_id = hook_data.get("session_id", "unknown")
    cwd = os.environ.get("CLAUDE_CWD", os.getcwd())

    # Build the review payload depending on tool type
    file_path = tool_input.get("path") or tool_input.get("file_path", "")
    if not file_path:
        sys.exit(0)     # nothing to review

    # Normalise to absolute path
    if not os.path.isabs(file_path):
        file_path = os.path.join(cwd, file_path)

    payload: dict = {
        "session_id": session_id,
        "tool_name": tool_name,
        "file_path": file_path,
        "cwd": cwd,
    }

    if tool_name == "Edit":
        payload["old_string"] = tool_input.get("old_string", "")
        payload["new_string"] = tool_input.get("new_string", "")

    elif tool_name == "Write":
        payload["content"] = tool_input.get("content", "")

    elif tool_name == "MultiEdit":
        # Represent each edit as combined old/new for display
        edits = tool_input.get("edits", [])
        payload["old_string"] = "\n---\n".join(e.get("old_string", "") for e in edits)
        payload["new_string"] = "\n---\n".join(e.get("new_string", "") for e in edits)
        payload["edits"] = edits

    else:
        # Unexpected tool — don't block
        sys.exit(0)

    # Contact the server
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
        # Server not running — fail open so Claude is not blocked
        print(
            f"sublime_review: server unreachable ({e}), allowing by default",
            file=sys.stderr,
        )
        sys.exit(0)
    except Exception as e:
        print(f"sublime_review: unexpected error ({e}), allowing by default", file=sys.stderr)
        sys.exit(0)

    decision = result.get("decision", "allow")
    reason = result.get("reason", "")

    if decision == "allow":
        sys.exit(0)
    else:
        deny_msg = f"Change to {file_path} was rejected by the reviewer."
        if reason:
            deny_msg += f" Reason: {reason}"
        print(deny_msg, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
