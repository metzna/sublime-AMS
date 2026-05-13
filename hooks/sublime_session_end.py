#!/usr/bin/env python3
"""
SessionEnd Hook: sublime_session_end.py
Releases all file locks held by this session when Claude Code exits.
"""

import json
import sys
import urllib.request
import urllib.error

SERVER_URL = "http://localhost:9876/unlock_session"


def main() -> None:
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw)
    except Exception as e:
        print(f"sublime_session_end: failed to parse stdin: {e}", file=sys.stderr)
        sys.exit(0)

    session_id = hook_data.get("session_id", "unknown")

    try:
        body = json.dumps({"session_id": session_id}).encode()
        req = urllib.request.Request(
            SERVER_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        released = result.get("released", [])
        if released:
            print(f"sublime_session_end: released locks for {session_id}: {released}", file=sys.stderr)
    except urllib.error.URLError:
        # Server not running — nothing to release
        pass
    except Exception as e:
        print(f"sublime_session_end: error ({e})", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
