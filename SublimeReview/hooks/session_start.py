#!/usr/bin/env python3
"""SessionStart hook: pre-registers the agent in the dashboard.

Fires when Claude Code starts or resumes a session, before any tool call.
Falls back to activity.py for sessions that were already running when
Sublime (and thus the hooks) came online.
"""

import json
import os
import sys
import urllib.request

SERVER_URL = "http://localhost:9876/activity"
TIMEOUT = 5


def main():
    try:
        hook_data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    session_id = hook_data.get("session_id", "unknown")
    source     = hook_data.get("source", "startup")  # startup|resume|clear
    cwd        = os.environ.get("CLAUDE_CWD", os.getcwd())

    payload = {
        "session_id": session_id,
        "event_type": "session_start",
        "source":     source,
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
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
</content>
</invoke>