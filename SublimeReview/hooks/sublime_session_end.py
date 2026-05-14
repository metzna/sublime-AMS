#!/usr/bin/env python3
"""
SessionEnd Hook: sublime_session_end.py
========================================
Called by Claude Code when a session ends (normal exit, Ctrl-C, etc.).
Releases all file locks held by this session on the review server.

Without this hook, locks would persist until their 10-minute timeout fires.
With this hook, the next agent can acquire the lock immediately after Claude
exits rather than waiting for the timeout.

Configure in .claude/settings.json:
  {
    "hooks": {
      "SessionEnd": [{
        "hooks": [{"type": "command",
                   "command": "python3 ~/.claude/hooks/sublime_session_end.py"}]
      }]
    }
  }

Note: if Claude Code crashes hard (SIGKILL, power loss) this hook will not
fire.  The 10-minute lock timeout acts as the fallback in that case.
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
        print("sublime_session_end: failed to parse stdin: {}".format(e), file=sys.stderr)
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
            print(
                "sublime_session_end: released {} lock(s) for session {}".format(
                    len(released), session_id),
                file=sys.stderr,
            )

    except urllib.error.URLError:
        # Server not running — nothing to release, carry on.
        pass
    except Exception as e:
        print("sublime_session_end: error ({})".format(e), file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
