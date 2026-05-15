#!/usr/bin/env python3
"""
PreToolUse hook (no matcher): registers every agent on first tool call
and keeps the dashboard last-action line current.

Exits 0 immediately — never blocks the tool call.
"""

import json
import os
import sys
import urllib.error
import urllib.request

SERVER_URL = "http://localhost:9876/activity"
TIMEOUT = 5


def _action(tool_name, tool_input, cwd):
    if tool_name in ("Edit", "MultiEdit", "Write"):
        fp = tool_input.get("path") or tool_input.get("file_path", "")
        if fp and not os.path.isabs(fp):
            fp = os.path.join(cwd, fp)
        verb = "writing" if tool_name == "Write" else "editing"
        return "{} {}".format(verb, os.path.basename(fp)) if fp else tool_name
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return "running: " + (cmd[:60] + "…" if len(cmd) > 60 else cmd)
    if tool_name in ("Read", "ReadFile"):
        fp = tool_input.get("file_path") or tool_input.get("path", "")
        return "reading " + os.path.basename(fp) if fp else "reading"
    if tool_name == "WebSearch":
        q = tool_input.get("query", "")
        return "searching: " + (q[:50] + "…" if len(q) > 50 else q)
    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        return "fetching: " + url[:60]
    return tool_name


def main():
    try:
        hook_data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    session_id = hook_data.get("session_id", "unknown")
    tool_name  = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input", {})
    cwd        = os.environ.get("CLAUDE_CWD", os.getcwd())

    payload = {
        "session_id": session_id,
        "action":     _action(tool_name, tool_input, cwd),
        "cwd":        cwd,
    }

    try:
        req = urllib.request.Request(
            SERVER_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT)
    except Exception:
        pass  # dashboard is best-effort; never block the agent

    sys.exit(0)


if __name__ == "__main__":
    main()
