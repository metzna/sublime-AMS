# SublimeReview – command classes.
#
# All sublime_plugin.WindowCommand / TextCommand subclasses live here.
# Sublime auto-discovers them by importing every top-level .py file in the
# package, so this module needs no explicit registration.
#
# Each command obtains the per-window _Manager via plugin._manager().
# Manager internals (_inline, _mu, _active, _wait_for_inline, _panel) are
# accessed directly for the toggle-inline command — these are intentionally
# package-private and not part of a public API.

import sublime
import sublime_plugin

from SublimeReview import plugin


class SublimeReviewSetContentCommand(sublime_plugin.TextCommand):
    """Internal command: replace entire view content."""
    def run(self, edit, text=""):
        self.view.erase(edit, sublime.Region(0, self.view.size()))
        self.view.insert(edit, 0, text)


class SublimeReviewAcceptCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = plugin._manager(self.window)
        if m:
            m.accept()


class SublimeReviewRejectCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = plugin._manager(self.window)
        if not m:
            return
        self.window.show_input_panel(
            "Rejection reason (optional, Enter to confirm, Escape to skip):",
            "",
            lambda reason: m.reject(reason.strip()),  # Enter
            None,
            lambda: m.reject(""),                     # Escape
        )


class SublimeReviewNextCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = plugin._manager(self.window)
        if m:
            m.cycle()


class SublimeReviewShowCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = plugin._manager(self.window)
        if m:
            m.show_current()

    def is_enabled(self):
        m = plugin._managers.get(self.window.id())
        return m is not None and m.has_pending()


class SublimeReviewToggleInlineCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = plugin._manager(self.window)
        if not m:
            return
        with m._mu:
            review = m._active
        if review is None or review.get("tool_name", "") not in ("Edit", "MultiEdit"):
            return
        if m._inline._phantom_set is not None:
            m._inline.clear()
            m._panel.show(review, compact=False, has_inline=False)
        else:
            ok = m._inline.show(review)
            if ok:
                m._panel.show(review, compact=True, has_inline=True)
            else:
                fp = review.get("file_path", "")
                loading = m._window.find_open_file(fp)
                if loading is not None and loading.is_loading():
                    m._wait_for_inline(loading, review, 0)
                else:
                    sublime.status_message("SublimeReview: could not show inline diff")
        self.window.run_command("show_panel", {"panel": "output." + plugin.PANEL_NAME})


class SublimeReviewUnlockFileCommand(sublime_plugin.WindowCommand):
    def run(self, file_path=""):
        if not file_path:
            v = self.window.active_view()
            file_path = v.file_name() if v else ""
        if not file_path:
            sublime.status_message("SublimeReview: no file selected")
            return
        m = plugin._manager(self.window)
        if m:
            m.unlock_file(file_path)


class SublimeReviewConnectCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = plugin._manager(self.window)
        if m:
            m.stop()
            m.start()
            sublime.status_message("SublimeReview: reconnecting...")


class SublimeAgentsDashboardCommand(sublime_plugin.WindowCommand):
    def run(self):
        m = plugin._manager(self.window)
        if m:
            m._dashboard.toggle()
