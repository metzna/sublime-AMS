"""
Diff panel rendering for the SublimeReview plugin.

Creates a read-only output panel with colored diff lines (green = addition,
red = deletion) using Sublime's scope-based colour system.
"""

import difflib
from typing import Optional

import sublime
import sublime_plugin

from . import settings as cfg

PANEL_NAME = "sublime_review_diff"


def _compute_diff(
    file_path: str,
    old_string: str,
    new_string: str,
    content: str,
    tool_name: str,
    context_lines: int,
) -> list[tuple[str, str]]:
    """
    Return a list of (line_text, scope_suffix) tuples.
    scope_suffix is one of: "addition", "deletion", "context", "header".
    """
    lines: list[tuple[str, str]] = []

    if tool_name == "Write":
        # Entire file is new content — show it all as additions
        lines.append((f"--- (new file)\n", "header"))
        lines.append((f"+++ {file_path}\n", "header"))
        for line in content.splitlines(keepends=True):
            lines.append((f"+{line}", "addition"))
        return lines

    # Edit / MultiEdit — unified diff
    old_lines = old_string.splitlines(keepends=True)
    new_lines = new_string.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=context_lines,
    ))

    if not diff:
        lines.append(("(no changes)\n", "context"))
        return lines

    for dl in diff:
        if dl.startswith("---") or dl.startswith("+++") or dl.startswith("@@"):
            lines.append((dl, "header"))
        elif dl.startswith("+"):
            lines.append((dl, "addition"))
        elif dl.startswith("-"):
            lines.append((dl, "deletion"))
        else:
            lines.append((dl, "context"))

    return lines


def _scope_for(suffix: str) -> str:
    """Map a diff role to a Sublime colour scope."""
    return {
        "addition": "markup.inserted",
        "deletion": "markup.deleted",
        "header": "markup.changed",
        "context": "comment",
    }.get(suffix, "comment")


class ReviewPanel:
    """Manages the diff output panel."""

    def __init__(self, window: sublime.Window) -> None:
        self._window = window
        self._view: Optional[sublime.View] = None

    def _ensure_view(self) -> sublime.View:
        if self._view is None or not self._view.is_valid():
            self._view = self._window.create_output_panel(PANEL_NAME)
            self._view.set_read_only(False)
            self._view.settings().set("sublime_review_panel", True)
            self._view.settings().set("gutter", True)
            self._view.settings().set("line_numbers", False)
            self._view.settings().set("word_wrap", False)
            self._view.settings().set("draw_white_space", "none")
            self._view.set_read_only(True)
        return self._view

    def show(
        self,
        review: dict,
    ) -> None:
        """Render a review into the panel and reveal it."""
        view = self._ensure_view()
        view.set_read_only(False)
        view.run_command("select_all")
        view.run_command("right_delete")

        file_path = review.get("file_path", "")
        tool_name = review.get("tool_name", "Edit")
        old_string = review.get("old_string", "")
        new_string = review.get("new_string", "")
        content = review.get("content", "")
        agent_label = review.get("agent_label", "")
        queue_position = review.get("queue_position", 1)
        queue_total = review.get("queue_total", 1)

        header = (
            f"{'─' * 60}\n"
            f"  Claude Review  │  {agent_label}  │  {tool_name}  │  "
            f"{queue_position}/{queue_total} in queue\n"
            f"  File: {file_path}\n"
            f"  [Enter] Accept   [Escape] Reject   [Tab] Next\n"
            f"{'─' * 60}\n"
        )

        diff_lines = _compute_diff(
            file_path,
            old_string,
            new_string,
            content,
            tool_name,
            cfg.diff_context_lines(),
        )

        # Write header as plain text
        view.run_command("append", {"characters": header, "force": True})

        # Write each diff line with scoped colouring
        for text, role in diff_lines:
            start = view.size()
            view.run_command("append", {"characters": text, "force": True})
            end = view.size()
            region = sublime.Region(start, end)
            scope = _scope_for(role)
            key = f"sr_{role}_{start}"
            view.add_regions(key, [region], scope, "", sublime.DRAW_NO_OUTLINE)

        view.set_read_only(True)
        view.settings().set("sublime_review_panel_focused", True)
        self._window.run_command("show_panel", {"panel": f"output.{PANEL_NAME}"})

    def hide(self) -> None:
        self._window.run_command("hide_panel", {"panel": f"output.{PANEL_NAME}"})

    def clear(self) -> None:
        if self._view and self._view.is_valid():
            view = self._view
            view.set_read_only(False)
            view.run_command("select_all")
            view.run_command("right_delete")
            view.set_read_only(True)
        self.hide()


# ─── Context key for keybindings ──────────────────────────────────────────────

class SublimeReviewPanelFocusedContext(sublime_plugin.EventListener):
    """Provides the 'sublime_review_panel_focused' context for keybindings."""

    def on_query_context(self, view, key, operator, operand, match_all):
        if key != "sublime_review_panel_focused":
            return None
        is_panel = view.settings().get("sublime_review_panel", False)
        if operator == sublime.OP_EQUAL:
            return is_panel == operand
        if operator == sublime.OP_NOT_EQUAL:
            return is_panel != operand
        return None
