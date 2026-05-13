"""Plugin settings helper."""

import sublime


SETTINGS_FILE = "SublimeReview.sublime-settings"


def get(key: str, default=None):
    return sublime.load_settings(SETTINGS_FILE).get(key, default)


def server_host() -> str:
    return get("server_host", "localhost")


def server_port() -> int:
    return int(get("server_port", 9877))


def auto_reconnect() -> bool:
    return bool(get("auto_reconnect", True))


def reconnect_delay() -> int:
    return int(get("reconnect_delay", 3))


def diff_context_lines() -> int:
    return int(get("diff_context_lines", 3))


def status_bar_prefix() -> str:
    return get("status_bar_prefix", "Claude Review")


def lock_icon() -> str:
    return get("lock_icon", "🔒")
