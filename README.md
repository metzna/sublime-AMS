# SublimeReview – Claude Code Review Plugin

A Sublime Text 4 plugin that intercepts Claude Code's `Edit`, `Write`, and
`MultiEdit` tool calls and lets you review each proposed file change before it
is written to disk.

For `Edit` and `MultiEdit`, the change is shown inline in the file itself: the
old text is highlighted red, and the proposed new text appears as a green
phantom below it.  For `Write`, a full diff panel opens at the bottom of the
editor with syntax highlighting.  Press **Enter** to accept or **Escape** to
reject.

Multiple simultaneous Claude agents are supported with per-file locking.

---

## How it works

```
Claude Code                 Hook Script              Review Server         Sublime Plugin
    │                           │                         │                      │
    │── Edit/Write/MultiEdit ──▶│                         │                      │
    │   (tool call blocked)     │── POST /review ────────▶│                      │
    │                           │   (blocks waiting)      │── WS: review_request▶│
    │                           │                         │                      │── inline diff / panel
    │                           │                         │                      │   user presses Enter
    │                           │                         │◀─ WS: decision ──────│
    │                           │◀── HTTP 200 (allow) ────│                      │
    │◀── tool call resumes ─────│                         │                      │
```

The hook script runs synchronously inside Claude Code's `PreToolUse` hook —
Claude is frozen and cannot proceed until the script exits.  The review server
coordinates between the hook (HTTP on port 9876) and the plugin (WebSocket on
port 9877).

**The server and hooks are fully automatic:**
- The plugin starts the server when Sublime loads and stops it on unload.
- The server writes hook entries into `~/.claude/settings.json` when the first
  Sublime window connects, and removes them when the last window closes.
- If Sublime crashes, the WebSocket disconnect triggers hook removal — Claude
  is never left permanently blocked by a missing Sublime window.

---

## Package layout

Everything lives inside the single `SublimeReview/` package directory:

```
SublimeReview/
  plugin.py                     ← plugin logic (WS client, inline diff, panel, commands)
  sublime_review_server.py      ← HTTP + WebSocket server (started by plugin on load)
  hooks/
    sublime_review.py           ← PreToolUse hook (blocks Claude until decision)
    sublime_session_end.py      ← SessionEnd hook (releases locks on Claude exit)
  Default.sublime-keymap        ← Enter=Accept, Escape=Reject, Tab=Cycle
  SublimeReview.sublime-settings
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Sublime Text 4** | Plugin uses ST4 APIs |
| **Python 3.7+** (system) | Must be callable as `python3`; server uses `asyncio.run()` |
| **`websockets` pip package** | `pip install websockets` — only the server needs it |
| **Claude Code** | Must have run at least once so `~/.claude/` exists |
| **Linux or macOS** | Windows not currently supported (path and command differences) |

The plugin itself runs inside Sublime's bundled Python 3.8 environment and
requires no additional packages — only stdlib.

---

## Installation

### 1. Install the Python dependency

```bash
pip install websockets
```

### 2. Install the Sublime Text package

**Recommended — symlink (edits in the repo are immediately live in Sublime):**

```bash
# Linux
ln -s /path/to/sublime-AMS/SublimeReview \
      ~/.config/sublime-text/Packages/SublimeReview

# macOS
ln -s /path/to/sublime-AMS/SublimeReview \
      ~/Library/Application\ Support/Sublime\ Text/Packages/SublimeReview
```

**Alternative — copy:**

```bash
# Linux
cp -r SublimeReview ~/.config/sublime-text/Packages/

# macOS
cp -r SublimeReview ~/Library/Application\ Support/Sublime\ Text/Packages/
```

### 3. Open Sublime Text

The plugin starts the review server automatically.  The status bar shows
`SublimeReview: connected` once the WebSocket connection is established.

No further setup is needed — hook entries are written into
`~/.claude/settings.json` automatically while Sublime is open and removed when
it closes.

---

## Usage

1. Open Sublime Text — the server starts and hooks are enabled automatically.
2. Run Claude Code on any project and give it a task that edits files.
3. When Claude attempts an edit:
   - **Edit / MultiEdit**: the old text is highlighted red in the file; the
     proposed replacement appears as a green block below it.  A compact header
     panel at the bottom shows the file name and queue position.
   - **Write**: a full diff panel opens with green/red syntax highlighting.
4. Press **Enter** to accept (Claude writes the file) or **Escape** to reject
   (Claude receives an error message and may try a different approach).
5. **Tab** cycles through queued reviews if multiple changes are pending.
6. Close Sublime — hooks are removed from `~/.claude/settings.json`
   automatically; Claude runs unintercepted until Sublime is reopened.

---

## Multi-Agent Support

Each Claude Code session has a unique `session_id`.  The server enforces
one-at-a-time access per file:

- If Agent A holds the lock on `auth.py`, Agent B's request for the same file
  is denied immediately with `"file locked by Agent-A"`.
- If Agent B submits an edit while Agent A's review is pending, and Agent A's
  edit is then accepted, Agent B's queued review is auto-cancelled with
  `"file was modified by another agent"`.  Agent B re-reads the updated file
  and retries with fresh content.

Locks are released in this priority order:

1. User accepts or rejects the review in Sublime.
2. `SessionEnd` hook fires when Claude Code exits cleanly.
3. 10-minute timeout (configurable via `LOCK_TIMEOUT` in the server).
4. Manual unlock: Command Palette → `SublimeReview: Unlock Current File`.

---

## Configuration

Edit settings via **Preferences → Package Settings → SublimeReview → Settings**:

| Key | Default | Description |
|---|---|---|
| `server_host` | `"localhost"` | Review server hostname |
| `server_port` | `9877` | Review server WebSocket port |
| `auto_reconnect` | `true` | Reconnect automatically on disconnect |
| `reconnect_delay` | `3` | Seconds between reconnect attempts |
| `diff_context_lines` | `3` | Context lines shown in the diff panel |
| `status_bar_prefix` | `"Claude Review"` | Status bar label |

Timeouts are set at the top of `sublime_review_server.py`:

| Constant | Default | Description |
|---|---|---|
| `REVIEW_TIMEOUT` | `300` s | How long Claude waits before auto-allowing a review |
| `LOCK_TIMEOUT` | `600` s | How long a lock is held before automatic release |

---

## Server Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/review` | Submit a review request (blocks until decision) |
| POST | `/unlock_session` | Release all locks held by a session |
| POST | `/unlock_file` | Release the lock on a specific file |
| GET | `/status` | JSON snapshot of current locks, queue, clients |

```bash
# Check what the server currently sees
curl -s http://localhost:9876/status | python3 -m json.tool
```

---

## Known Limitations

**Claude can bypass review via Bash.**
The hook only intercepts `Edit`, `Write`, and `MultiEdit`.  Claude can write
files through the `Bash` tool (`echo`, `cat`, `python3 -c`, etc.) without
triggering a review.

**Timeout auto-approves.**
If you do not respond within `REVIEW_TIMEOUT` (default 5 minutes), the change
is automatically approved.  This ensures Claude is never permanently blocked
when Sublime is not available.

**Server loses state on restart.**
Locks and queued reviews are held in memory.  If the server restarts while
reviews are pending, those reviews auto-approve via fail-open in the hook
script.

**Windows not supported.**
The hook commands written into `settings.json` use Unix absolute paths and
`python3`.  Supporting Windows would require detecting the platform and
adjusting both the command template and the Python executable name.

---

## Architecture Notes

**Why HTTP for hook↔server and WebSocket for server↔Sublime?**
The hook script needs a simple blocking request-response — HTTP with `urllib`
(no extra dependencies) is ideal.  The Sublime plugin needs the server to push
notifications without polling — WebSocket is the natural fit.

**Why in-memory state?**
The server is a local coordinator for a working session.  Locks and queued
reviews are short-lived by design; persistence across restarts adds complexity
with little benefit.

**Why a single `plugin.py`?**
Sublime Text's plugin host silently skips packages that fail to import, making
multi-file packages with relative imports hard to debug.  A single file
eliminates all import complexity.
