#!/usr/bin/env python3
"""SubagentStop hook: marks a subagent as finished in the dashboard."""

import json
import sys
import urllib.request

SERVER_URL = "http://localhost:9876/subagent/stop"
TIMEOUT = 5


def main():
    try:
        hook_data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    agent_id   = hook_data.get("agent_id", "")
    session_id = hook_data.get("session_id", "unknown")

    if not agent_id:
        sys.exit(0)

    try:
        req = urllib.request.Request(
            SERVER_URL,
            data=json.dumps({
                "parent_session_id": session_id,
                "agent_id":          agent_id,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
