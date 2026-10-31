"""Per-platform chat mute state for the LCARS dashboard toggles.

Independent on/off switch for Twitch chat input. Mute = that platform's
incoming chat is dropped at the drain (never processed, never replied to).
Console, mic and dashboard-authored commands are unaffected — only the
platform's own chat input is gated.

Backed by a thread-safe in-memory store so the status-feed thread and
the main loop can read/write it without a lock dance.
"""
import threading

_lock = threading.Lock()
_mutes = {"twitch": False}
_PLATFORMS = ("twitch",)


def is_chat_muted(platform):
    """True when the given platform's chat input is muted (dropped)."""
    with _lock:
        return bool(_mutes.get(platform, False))


def set_chat_muted(platform, muted):
    """Set the mute flag for a platform. Returns False on unknown platform."""
    if platform not in _PLATFORMS:
        return False
    with _lock:
        _mutes[platform] = bool(muted)
    return True


def chat_mute_snapshot():
    with _lock:
        return dict(_mutes)
