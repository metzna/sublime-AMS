"""
Tab and status-bar lock indicators for the SublimeReview plugin.

- Adds the lock icon before a file's tab name while its lock is active.
- Updates the status bar with pending/waiting review counts.
"""

from typing import Optional
import sublime

from . import settings as cfg


class LockIndicator:
    """Manages tab name decorations and the status bar message."""

    def __init__(self, window: sublime.Window) -> None:
        self._window = window
        # file_path → original tab name
        self._locked_tabs: dict[str, Optional[str]] = {}

    # ── Lock management ───────────────────────────────────────────────────────

    def apply_locks(self, locks: dict) -> None:
        """
        Synchronise the visual state with the server's lock dict.
        locks: {file_path: {"agent_label": str, "since": float}}
        """
        current_paths = set(locks.keys())
        previous_paths = set(self._locked_tabs.keys())

        for fp in current_paths - previous_paths:
            self._lock_tab(fp)

        for fp in previous_paths - current_paths:
            self._unlock_tab(fp)

    def clear_all(self) -> None:
        for fp in list(self._locked_tabs):
            self._unlock_tab(fp)

    # ── Status bar ────────────────────────────────────────────────────────────

    def update_status(self, pending: int, waiting: int = 0) -> None:
        prefix = cfg.status_bar_prefix()
        if pending == 0:
            for view in self._window.views():
                view.erase_status("sublime_review")
        else:
            msg = f"{prefix}: {pending} pending"
            if waiting:
                msg += f" ({waiting} waiting)"
            for view in self._window.views():
                view.set_status("sublime_review", msg)

    def clear_status(self) -> None:
        self.update_status(0)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _find_view_for(self, file_path: str) -> Optional[sublime.View]:
        for view in self._window.views():
            if view.file_name() == file_path:
                return view
        return None

    def _lock_tab(self, file_path: str) -> None:
        view = self._find_view_for(file_path)
        icon = cfg.lock_icon()
        if view:
            original = view.name() or view.file_name() or file_path
            self._locked_tabs[file_path] = original
            view.set_name(f"{icon} {original}")
        else:
            self._locked_tabs[file_path] = None

    def _unlock_tab(self, file_path: str) -> None:
        original = self._locked_tabs.pop(file_path, None)
        view = self._find_view_for(file_path)
        if view and original is not None:
            view.set_name(original)
        elif view:
            # Restore to empty (Sublime will show filename automatically)
            view.set_name("")
