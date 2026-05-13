# SublimeReview – Claude Code Review Plugin

A system that intercepts Claude Code's `Edit`, `Write`, and `MultiEdit` tool
calls and lets you review each change in Sublime Text before it is written to
disk. Supports multiple simultaneous Claude agents with file-level locking.

## Components

```
sublime_review_server.py        ← persistent HTTP + WebSocket coordinator
hooks/
  sublime_review.py             ← PreToolUse hook (called by Claude Code)
  sublime_session_end.py        ← SessionEnd hook (cleans up locks on exit)
SublimeReview/                  ← Sublime Text 4 package
  __init__.py
  plugin.py                     ← WebSocket client, commands, queue management
  review_panel.py               ← diff rendering in an output panel
  lock_indicator.py             ← tab name decoration + status bar
  settings.py                   ← settings helper
  Default.sublime-keymap        ← Enter=Accept, Escape=Reject, Tab=Next
  SublimeReview.sublime-settings
claude_settings.json            ← example .claude/settings.json (copy & merge)
```

## Installation

### 1. Install the server dependency

```bash
pip3 install websockets
```

### 2. Copy hook scripts

```bash
mkdir -p ~/.claude/hooks
cp hooks/sublime_review.py       ~/.claude/hooks/
cp hooks/sublime_session_end.py  ~/.claude/hooks/
chmod +x ~/.claude/hooks/sublime_review.py
chmod +x ~/.claude/hooks/sublime_session_end.py
```

### 3. Copy the server

```bash
cp sublime_review_server.py ~/.claude/
```

### 4. Configure Claude Code hooks

Merge the contents of `claude_settings.json` into `~/.claude/settings.json`.

### 5. Install the Sublime Text package

```bash
# macOS
cp -r SublimeReview \
  ~/Library/Application\ Support/Sublime\ Text/Packages/

# Linux
cp -r SublimeReview \
  ~/.config/sublime-text/Packages/
```

Install the WebSocket client library inside Sublime's Python environment:

```bash
# Sublime Text 4 ships its own Python 3.8; use Package Control or:
# Tools → Developer → Python Console, then:
# import subprocess; subprocess.run(["pip3", "install", "websocket-client"])
```

### 6. Start the server

```bash
python3 ~/.claude/sublime_review_server.py &
```

For a persistent service, create a `launchd` (macOS) or `systemd` (Linux)
unit pointing at the script above. The server must be running before Claude
Code starts.

## Usage

1. Start the server (step 6 above).
2. Open Sublime Text — the plugin connects automatically.
3. Start Claude Code in any project.
4. When Claude tries to edit a file:
   - The file opens in Sublime Text, its tab gets a 🔒 icon.
   - A diff panel appears at the bottom showing the proposed change.
   - Press **Enter** to accept, **Escape** to reject, **Tab** to cycle to the
     next pending review.
5. Claude receives the decision immediately and continues (or adjusts its
   approach if the change was rejected).

## Multi-Agent Support

Each Claude Code session gets a unique `session_id`. The server enforces
one-at-a-time access per file:

- If Agent A holds the lock on `auth.py`, Agent B's request for the same
  file is denied immediately with the message
  `"file locked by Agent-<id>"`.
- Locks are released after each review decision, when a session ends (via the
  `SessionEnd` hook), after a configurable timeout (default: 10 minutes), or
  manually from Sublime via **Tools → SublimeReview → Unlock Current File**.

## Server Endpoints

| Method | Path              | Description                        |
|--------|-------------------|------------------------------------|
| POST   | `/review`         | Submit a review request (blocks)   |
| POST   | `/unlock_session` | Release all locks for a session    |
| POST   | `/unlock_file`    | Release lock for one file          |
| GET    | `/status`         | JSON snapshot of locks and queue   |

### Quick test without Sublime

```bash
# Start server
python3 ~/.claude/sublime_review_server.py &

# Submit a review (simulates what the hook script does)
curl -s -X POST http://localhost:9876/review \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"test","tool_name":"Edit","file_path":"/tmp/foo.py",
       "old_string":"x = 1","new_string":"x = 2"}' | jq .

# In another terminal, accept it:
curl -s http://localhost:9876/status | jq .
# Then connect with a WS client and send:
# {"type":"review_decision","review_id":"<id>","decision":"allow"}
```

## Configuration

Edit `SublimeReview.sublime-settings` (accessible via
**Preferences → Package Settings → SublimeReview → Settings**):

| Key                 | Default           | Description                              |
|---------------------|-------------------|------------------------------------------|
| `server_host`       | `"localhost"`     | Review server hostname                   |
| `server_port`       | `9877`            | Review server WebSocket port             |
| `auto_reconnect`    | `true`            | Reconnect on disconnect                  |
| `reconnect_delay`   | `3`               | Seconds between reconnect attempts       |
| `diff_context_lines`| `3`               | Context lines shown around diff hunks    |
| `lock_icon`         | `"🔒"`            | Icon prepended to locked tab names       |

The hook timeout (how long Claude waits for a review decision before
auto-allowing) is set in `claude_settings.json` under
`hooks.PreToolUse[].hooks[].timeout` (default: `300` seconds).
