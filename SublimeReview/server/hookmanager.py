"""
Sublime Review Server — hook installation / removal.

Writes hook entries into ~/.claude/settings.json when the first Sublime
client connects, and removes them when the last disconnects.  All entries
written by this plugin are recognised by script basename so that stale
entries from a different installation path are also cleaned up.

State is kept in the JSON file itself; this module holds no module-level
state of its own.
"""

import json
import logging
import os
import tempfile

import config

log = logging.getLogger("server.hooks")


# ─── JSON helpers ────────────────────────────────────────────────────────────

def _first_hook_command(entry: dict) -> str:
    hooks = entry.get("hooks", [])
    return hooks[0].get("command", "") if hooks else ""


def _hook_basename(entry: dict) -> str:
    """Return the script filename from a hook entry (e.g. 'sublime_review.py')."""
    cmd = _first_hook_command(entry)
    parts = cmd.split() if cmd else []
    return os.path.basename(parts[-1]) if parts else ""


def is_our_hook(entry: dict) -> bool:
    return _hook_basename(entry) in config.OUR_HOOK_BASENAMES


def _atomic_write_json(path: str, data) -> None:
    dir_ = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _purge_our_hooks(hooks: dict) -> None:
    """Remove all entries that belong to this plugin (matched by script basename)."""
    for key in ("PreToolUse", "PostToolUse", "SessionStart", "SessionEnd",
                "SubagentStart", "SubagentStop"):
        hooks[key] = [e for e in hooks.get(key, []) if not is_our_hook(e)]


# ─── Public API ──────────────────────────────────────────────────────────────

def enable_hooks() -> None:
    try:
        with open(config.SETTINGS_PATH) as f:
            data = json.load(f)
    except Exception:
        data = {}
    hooks = data.setdefault("hooks", {})
    # Remove any stale entries from other installation paths before adding ours
    _purge_our_hooks(hooks)
    hooks.setdefault("PreToolUse", []).extend([
        {"hooks": [{"type": "command", "command": config.ACTIVITY_CMD, "timeout": 10}]},
        {"matcher": "Edit|Write|MultiEdit",
         "hooks": [{"type": "command", "command": config.REVIEW_CMD, "timeout": 300}]},
    ])
    hooks.setdefault("PostToolUse", []).append(
        {"hooks": [{"type": "command", "command": config.POST_TOOL_CMD, "timeout": 10}]}
    )
    hooks.setdefault("SessionStart", []).append(
        {"hooks": [{"type": "command", "command": config.SESSION_START_CMD, "timeout": 10}]}
    )
    hooks.setdefault("SessionEnd", []).append(
        {"hooks": [{"type": "command", "command": config.SESSION_END_CMD}]}
    )
    hooks.setdefault("SubagentStart", []).append(
        {"hooks": [{"type": "command", "command": config.SUBAGENT_START_CMD, "timeout": 10}]}
    )
    hooks.setdefault("SubagentStop", []).append(
        {"hooks": [{"type": "command", "command": config.SUBAGENT_STOP_CMD, "timeout": 10}]}
    )
    _atomic_write_json(config.SETTINGS_PATH, data)
    log.info("Hooks enabled")


def disable_hooks() -> None:
    try:
        with open(config.SETTINGS_PATH) as f:
            data = json.load(f)
    except Exception:
        return
    hooks = data.get("hooks", {})
    _purge_our_hooks(hooks)
    for key in ("PreToolUse", "PostToolUse", "SessionStart", "SessionEnd",
                "SubagentStart", "SubagentStop"):
        if not hooks.get(key):
            hooks.pop(key, None)
    if not hooks:
        data.pop("hooks", None)
    _atomic_write_json(config.SETTINGS_PATH, data)
    log.info("Hooks disabled")
