# SublimeReview – settings access helpers.
#
# Thin wrappers around sublime.load_settings() so the rest of the plugin
# code reads each value through a typed accessor with a default.  Each call
# is lazy — settings are looked up on use, not at import time, so a settings
# file edit takes effect on the next access without restarting Sublime.

import sublime

SETTINGS_FILE = "SublimeReview.sublime-settings"


def _s(key, default=None):
    return sublime.load_settings(SETTINGS_FILE).get(key, default)


def _host():            return _s("server_host", "localhost")
def _port():            return int(_s("server_port", 9877))
def _auto_reconnect():  return bool(_s("auto_reconnect", True))
def _reconnect_delay(): return int(_s("reconnect_delay", 3))
def _context_lines():   return int(_s("diff_context_lines", 3))
def _status_prefix():   return _s("status_bar_prefix", "Claude Review")
def _lock_icon():       return _s("lock_icon", "[locked]")
