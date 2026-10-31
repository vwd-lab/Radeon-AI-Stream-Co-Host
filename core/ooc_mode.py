"""Persistent, community-toggleable OOC (out-of-character) mode.

A single global stream-wide flag (in memory) that any chatter can flip with
"ooc" / "ic" on any platform. While OOC, the bot drops the persona and replies as
a direct AI assistant. The mode auto-resets to Character after
OOC_IDLE_TIMEOUT_S of silence, and the toggle is cooldown-guarded so one person
can't flip-flop it every message.

The actual prompt swap happens in main.process_message; this module owns the
transition state and the "what text should the toggle produce" logic.
"""
import time

import config


class OocMode:
    """Global OOC mode state. Thread access is serialized by the calling
    thread pattern (the bot's main loop is effectively single-threaded), but a
    lock keeps the status-feed poller safe if it ever reads concurrently."""

    def __init__(self, enabled=True):
        self.enabled = bool(enabled)
        self.active = False
        self.last_set_ts = 0.0
        self.last_toggle_ts = 0.0
        self.engaged_by = None
        self._lock = None
        try:
            import threading
            self._lock = threading.Lock()
        except Exception:
            pass

    # ---- transition helpers ----------------------------------------------

    def _now(self):
        return time.time()

    def expire_if_idle(self):
        """Return to Character after OOC_IDLE_TIMEOUT_S of no activity. Returns
        True if the mode just auto-reset (caller may want to log/notify)."""
        if not self.enabled or not self.active:
            return False
        if self._now() - self.last_set_ts >= config.OOC_IDLE_TIMEOUT_S:
            self.active = False
            self.engaged_by = None
            return True
        return False

    def is_ooc(self):
        self.expire_if_idle()
        return self.enabled and self.active

    def touch(self):
        """Called whenever a message is processed so the idle clock reflects
        activity, never resetting while OOC (last_set_ts only moves if we're
        active, so idle counts from the last real interaction)."""
        if self.enabled and self.active:
            self.last_set_ts = self._now()

    def _can_toggle(self):
        return (self._now() - self.last_toggle_ts) >= config.OOC_TOGGLE_COOLDOWN_S

    def attempt_toggle(self, username):
        """Flip the mode. Returns a tuple (new_state, action_text) where
        action_text is the spoken/replied line, or None if cooldown blocked it.
        new_state is True (OOC) / False (Character) only on success."""
        if not self.enabled:
            return None, "OOC mode is disabled."
        if not self._can_toggle():
            return None, None
        self.expire_if_idle()
        self.active = not self.active
        self.last_toggle_ts = self._now()
        self.last_set_ts = self._now()
        self.engaged_by = username
        if self.active:
            return True, "Out of character. Speakin' straight now — no act."
        return False, "Back in character. The mask is on."

    def status_line(self):
        if self.expire_if_idle():
            return "MODE: CHARACTER"
        if self.enabled and self.active:
            return "MODE: OOC"
        return "MODE: CHARACTER"

    # ---- serialization ---------------------------------------------------

    def snapshot(self):
        self.expire_if_idle()
        return {
            "active": bool(self.enabled and self.active),
            "engaged_by": self.engaged_by,
            "enabled": bool(self.enabled),
        }
