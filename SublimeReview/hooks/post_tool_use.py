#!/usr/bin/env python3
"""PostToolUse hook: records tool completion to the audit log via server."""

import json
import os
import sys
import urllib.request

SERVER_URL = "http://localhost:9876/activity"
TIMEOUT = 5
_MAX_RESP = 300


def _result_summary(tool_name, tool_input, tool_response):
    resp = (str(tool_response or "")).strip()
    err  = "error" in resp.lower() or "traceback" in resp.lower()
    if tool_name in ("Edit", "MultiEdit"):
        fp = tool_input.get("path") or tool_input.get("file_path", "")
        bn = os.path.basename(fp) if fp else ""
        return ("edited " + bn + (" ✗" if err else " ✓")) if bn else None
    if tool_name == "Write":
        fp = tool_input.get("path") or tool_input.get("file_path", "")
        bn = os.path.basename(fp) if fp else ""
        return ("wrote " + bn + (" ✗" if err else " ✓")) if bn else None
    if tool_name == "Bash":
        short = resp[:80].replace("\n", " ")
        return "bash: " + (short + "…" if len(resp) > 80 else short)
    return None  # skip uninteresting tools


def main():
    try:
        hook_data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    session_id    = hook_data.get("session_id", "unknown")
    tool_name     = hook_data.get("tool_name", "")
    tool_input    = hook_data.get("tool_input", {})
    tool_response = hook_data.get("tool_response", "")
    cwd           = os.environ.get("CLAUDE_CWD", os.getcwd())

    summary = _result_summary(tool_name, tool_input, tool_response)

    # Always POST so the server can write the audit log entry
    payload = {
        "session_id":     session_id,
        "event_type":     "post_tool_use",
        "tool_name":      tool_name,
        "result_summary": summary,
        "result_snippet": str(tool_response or "")[:_MAX_RESP],
        "cwd":            cwd,
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
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
