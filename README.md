# SublimeReview – Claude Code Review Plugin

A system that intercepts Claude Code's `Edit`, `Write`, and `MultiEdit` tool
calls and lets you review each proposed file change in Sublime Text before it
is written to disk.  Each change appears as a unified diff in a panel at the
bottom of the editor.  Press **Enter** to accept or **Escape** to reject.
Multiple simultaneous Claude agents are supported with per-file locking.

---

## How it works

```
Claude Code                 Hook Script              Review Server         Sublime Plugin
    │                           │                         │                      │
    │── Edit/Write/MultiEdit ──▶│                         │                      │
    │   (tool call blocked)     │── POST /review ────────▶│                      │
    │                           │   (blocks waiting)      │── WS: review_request▶│
    │                           │                         │                      │── shows diff panel
    │                           │                         │                      │   user presses Enter
    │                           │                         │◀─ WS: decision ──────│
    │                           │◀── HTTP 200 (allow) ────│                      │
    │◀── tool call resumes ─────│                         │                      │
```

The hook script runs synchronously inside Claude Code's `PreToolUse` hook —
Claude is blocked and cannot proceed until the script exits.  The review server
coordinates between hook scripts (HTTP) and the Sublime plugin (WebSocket).

---

## Components

```
sublime_review_server.py        ← persistent HTTP + WebSocket server
hooks/
  sublime_review.py             ← PreToolUse hook (blocks Claude until decision)
  sublime_session_end.py        ← SessionEnd hook (releases locks on Claude exit)
SublimeReview/                  ← Sublime Text 4 package
  plugin.py                     ← all plugin logic (WS client, panel, commands)
  Default.sublime-keymap        ← Enter=Accept, Escape=Reject, Tab=Cycle
  SublimeReview.sublime-settings
claude_settings.json            ← example .claude/settings.json (copy & merge)
```

---

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
```

### 3. Copy the server

```bash
cp sublime_review_server.py ~/.claude/
```

### 4. Configure Claude Code hooks

Merge the contents of `claude_settings.json` into `~/.claude/settings.json`.
The hooks section wires `sublime_review.py` into `PreToolUse` (with a 300-second
timeout) and `sublime_session_end.py` into `SessionEnd`.

### 5. Install the Sublime Text package

```bash
# Linux
cp -r SublimeReview ~/.config/sublime-text/Packages/

# macOS
cp -r SublimeReview ~/Library/Application\ Support/Sublime\ Text/Packages/
```

The package uses only Python stdlib — no extra dependencies needed inside
Sublime's sandboxed Python environment.

### 6. Start the server

```bash
python3 ~/.claude/sublime_review_server.py &
```

The server must be running **before** Claude Code starts.  On subsequent
sessions you can add a check to your shell startup:

```bash
pgrep -f sublime_review_server.py || python3 ~/.claude/sublime_review_server.py &
```

For a persistent service, create a `systemd` user unit (Linux) or `launchd`
plist (macOS) pointing at the script.

---

## Usage

1. Start the review server (step 6 above).
2. Open Sublime Text — the plugin connects automatically and shows
   `SublimeReview: connected` in the status bar.
3. Start Claude Code in any project and give it a task that edits files.
4. When Claude tries to edit a file:
   - A diff panel opens at the bottom of Sublime showing the proposed change.
   - The status bar shows `Claude Review: 1 pending`.
   - Press **Enter** to accept (Claude writes the file) or **Escape** to reject
     (Claude gets an error message and may try a different approach).
5. If you stop Claude mid-review, pressing Enter or Escape on a stale panel
   is harmless — the server logs a warning and discards the decision.

---

## Multi-Agent Support

Each Claude Code session has a unique `session_id`.  The server enforces
one-at-a-time access per file:

- If Agent A holds the lock on `auth.py`, Agent B's request for the same
  file is denied immediately with `"file locked by Agent-A"`.
- If Agent B submits an edit while Agent A's review is **pending**, and Agent
  A's edit is then **accepted**, Agent B's queued review is auto-cancelled with
  `"file was modified by another agent"`.  Claude Code for Agent B re-reads
  the updated file and retries with fresh content.

Locks are released in this priority order:

1. User accepts or rejects the review in Sublime.
2. `SessionEnd` hook fires when Claude Code exits cleanly.
3. 10-minute timeout (configurable via `LOCK_TIMEOUT` in the server).
4. Manual unlock: open the Command Palette → `SublimeReview: Unlock Current File`.

---

## Server Endpoints

| Method | Path              | Description                                      |
|--------|-------------------|--------------------------------------------------|
| POST   | `/review`         | Submit a review request (blocks until decision)  |
| POST   | `/unlock_session` | Release all locks held by a session              |
| POST   | `/unlock_file`    | Release the lock on a specific file              |
| GET    | `/status`         | JSON snapshot of current locks, queue, clients   |

### Quick test without Sublime

```bash
# Start server
python3 ~/.claude/sublime_review_server.py &

# Submit a fake review (simulates what the hook script does)
curl -s -X POST http://localhost:9876/review \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "test",
    "tool_name": "Edit",
    "file_path": "/tmp/foo.py",
    "old_string": "x = 1",
    "new_string": "x = 2"
  }' | python3 -m json.tool
# (blocks until a WS client sends a decision, or times out after 5 min)

# Check status in another terminal
curl -s http://localhost:9876/status | python3 -m json.tool
```

---

## Configuration

Edit `SublimeReview.sublime-settings` via
**Preferences → Package Settings → SublimeReview → Settings**:

| Key                  | Default            | Description                                     |
|----------------------|--------------------|-------------------------------------------------|
| `server_host`        | `"localhost"`      | Review server hostname                          |
| `server_port`        | `9877`             | Review server WebSocket port                    |
| `auto_reconnect`     | `true`             | Reconnect automatically on disconnect           |
| `reconnect_delay`    | `3`                | Seconds between reconnect attempts              |
| `diff_context_lines` | `3`                | Context lines shown around each diff hunk       |
| `lock_icon`          | `"[locked]"`       | Text prepended to locked tab names              |
| `status_bar_prefix`  | `"Claude Review"`  | Status bar label                                |

The **review timeout** (how long Claude waits before auto-allowing) is set in
`claude_settings.json` under `hooks.PreToolUse[].hooks[].timeout` (default:
`300` seconds / 5 minutes).

The **lock timeout** (how long a lock is held without a decision before the
server releases it automatically) is `LOCK_TIMEOUT` at the top of
`sublime_review_server.py` (default: 600 seconds / 10 minutes).

---

## Known Limitations

### Claude can bypass the review using Bash
The `PreToolUse` hook only intercepts `Edit`, `Write`, and `MultiEdit` tool
calls.  Claude can write files using shell commands via the `Bash` tool
(e.g. `echo "..." > file.py`, `cat > file.py << EOF`, `python3 -c "open(...).write(...)"`).
These bypass the review entirely.  If bypassing is a concern, consider also
hooking `Bash` calls — though this is noisy and blocks all shell commands.

### Review timeout auto-approves
If you do not interact with Sublime within the timeout window (default 5
minutes), the pending review is automatically approved and Claude writes the
file.  The timeout exists so Claude is not permanently blocked when Sublime is
not available.  Increase `timeout` in `claude_settings.json` if you need more
time per review.

### Server loses state on restart
Locks and the pending review queue are held in memory.  If the server is
restarted while reviews are pending, those reviews auto-approve (the hook
script gets a connection error and fails open).  This is intentional — Claude
should not be permanently blocked by a crashed server.

### Stale Agent 2 panel after auto-cancel (tracked issue)
When Agent 2's queued review is auto-cancelled because Agent 1 modified the
file, the Sublime panel for Agent 2 may remain open and empty until the user
manually dismisses it.  Pressing Enter or Escape on the stale panel is safe.
Agent 2's Claude Code process receives the deny response from the server
independently of the Sublime UI state.

### Python 3.3 plugin host
Sublime Text 4 loads packages in its bundled Python 3.3 interpreter by default
(despite shipping a Python 3.8 host as well).  The `.python-version` file in
the package requests 3.8, but on some builds this is not honoured.  The plugin
is written to be fully compatible with Python 3.3 as a fallback.

### Multi-agent: stale edits after concurrent modification
If two agents both compute edits for the same file before either review is
shown, the second agent's edit is based on the original file content.  After
the first edit is accepted and the file changes, the second edit is
auto-cancelled.  Claude Code for the second agent re-reads the file and
retries, which adds one round-trip of latency.

---

## Architecture Notes

**Why HTTP for hook↔server and WebSocket for server↔Sublime?**
The hook script needs a simple blocking request-response pattern — HTTP with
`urllib` (no dependencies) is ideal.  The Sublime plugin needs the server to
push notifications to it without polling — WebSocket is the natural fit.

**Why in-memory state?**
The server is a local coordinator that lives for the duration of a working
session.  Persistence across restarts adds complexity with little benefit since
locks and queued reviews are inherently short-lived.

**Why a single `plugin.py`?**
Sublime Text's plugin host silently skips packages that fail to import, making
multi-file packages with relative imports hard to debug.  A single file
eliminates all import complexity and is the most reliable structure for a
plugin of this size.
