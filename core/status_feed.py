"""Read-only status feed for the separate-process LCARS dashboard.

The bot runs a single StatusFeed (module-level singleton). A background daemon
thread periodically writes a snapshot to data/status.json using an atomic
temp-file + rename. The dashboard polls that file. Nothing here mutates the bot;
it only reads existing objects and exposes their state.
"""
import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
import config
from core.voice_mute import is_voice_muted
from core.chat_mute import chat_mute_snapshot
from core.twitch_live import is_enabled as twitch_live_tracking_enabled



def model_badge(model_name):
    """Turn a model name into a compact badge.

    gemini-3.5-flash-lite -> '3.5'
    gemma3:4b         -> 'gemma3'
    """
    if not model_name:
        return ""
    name = model_name.lower()
    if "gemma" in name:
        return name.split(":")[0]
    m = re.search(r"gemini[-/]?([0-9]+[._][0-9]+)", name)
    if m:
        return m.group(1).replace("_", ".")
    return name.split(":")[0]


class StatusFeed:
    def __init__(self, path=None, max_activity=100, interval=0.5):
        self.path = path or config.STATUS_FILE
        self.max_activity = max_activity
        self.interval = interval
        self._lock = threading.Lock()
        self._activity = deque(maxlen=max_activity)
        self._last_payload = None
        self._last_write = 0.0
        # Force a rewrite this often even when nothing meaningful changed, so the
        # dashboard's uptime readout keeps advancing.
        self.heartbeat_s = 10.0
        self._transmission = ""
        self._latency = {
            "total_ms": None,
            "brain_ms": None,
            "tts_ms": None,
            "play_ms": None,
            "avg_ms": None,
            "source": "",
        }
        self._latency_alpha = 0.3
        self._started = time.time()
        self._writer = None
        self._running = False
        self._objects = {}

    # --- state setters (called from the bot) ---
    def record(self, text):
        with self._lock:
            self._activity.append({"ts": datetime.now().strftime("%H:%M:%S"), "text": text})

    def set_transmission(self, text):
        with self._lock:
            self._transmission = text

    def set_latency(self, total_ms, brain_ms=None, tts_ms=None, play_ms=None, source=""):
        """Record a completed full-pipeline run from the bot.

        Keeps the most recent stage timings plus an exponentially-weighted
        moving average of the total so the dashboard doesn't jump on spikes.
        """
        with self._lock:
            prev = self._latency.get("avg_ms")
            if total_ms is not None:
                self._latency["avg_ms"] = (
                    total_ms if prev is None else
                    self._latency_alpha * total_ms + (1 - self._latency_alpha) * prev
                )
            self._latency["total_ms"] = total_ms
            self._latency["brain_ms"] = brain_ms
            self._latency["tts_ms"] = tts_ms
            self._latency["play_ms"] = play_ms
            if source:
                self._latency["source"] = source

    def bind(self, **objects):
        self._objects.update(objects)

    # --- snapshot assembly ---
    def _snapshot(self):
        live = self._objects
        ai = live.get("ai_engine")
        mem = live.get("memory_db")
        passive = live.get("passive")
        ooc = live.get("ooc_mode")

        tokens_used = tokens_limit = tokens_remaining = None
        if mem is not None:
            try:
                tokens_used = mem.get_tokens_used_today()
                tokens_limit = config.DAILY_TOKEN_LIMIT
                tokens_remaining = max(0, tokens_limit - tokens_used)
            except Exception:
                pass

        model = ""
        is_local = False
        rotation = []
        if ai is not None:
            try:
                model = ai.current_model() or ""
                is_local = bool(getattr(ai, "_using_local", False))
                if not is_local:
                    # Local-only mode has no DualEngine flag to read.
                    from core.ai_engine import LocalAiEngine
                    is_local = isinstance(ai, LocalAiEngine)
                rotation = list(getattr(ai, "_models", []) or [])
            except Exception:
                model, is_local, rotation = "", False, []

        # Daily RPD budget per Gemini channel: used / limit / remaining, aligned
        # 1:1 with `rotation` (uncapped slots — e.g. the local fallback — get
        # used=None so the dashboard shows them as unlimited).
        channel_rpd = []
        for c in rotation:
            cap = config.CHANNEL_RPD.get(c)
            used = None
            if cap and mem is not None:
                get = getattr(mem, "get_channel_usage", None)
                if get:
                    try:
                        used = get(c)
                    except Exception:
                        used = None
            channel_rpd.append({
                "name": c,
                "limit": cap,
                "used": used,
                "remaining": max(0, cap - used) if (cap and used is not None) else None,
            })

        ears_state = "off"
        wake_words = config.WAKE_WORDS or []
        if passive is not None:
            try:
                ears_state = getattr(passive, "state", "off") or "off"
            except Exception:
                ears_state = "off"

        with self._lock:
            activity = list(self._activity)
            transmission = self._transmission
            latency = dict(self._latency)

        reset_time = None
        if ai is not None:
            try:
                reset_time = ai._next_reset_time(config.RESET_TZ)
            except Exception:
                reset_time = None

        reset_epoch = None
        if ai is not None:
            try:
                reset_epoch = ai._next_reset_epoch(config.RESET_TZ)
            except Exception:
                reset_epoch = None

        # Optional integration status (bound from main()).
        discord_connected = bool(live.get("discord_connected", False))
        obs_enabled = bool(live.get("obs_enabled", False))
        kick_enabled = bool(live.get("kick_enabled", False))

        # Is the streamer actually live right now? Read the cached value from
        # the background poller (see core/twitch_live). None = unknown.
        twitch_live = None
        if config.TWITCH_CLIENT_ID:
            try:
                from core.twitch_live import is_live
                twitch_live = is_live()
            except Exception:
                twitch_live = None

        return {
            "model": model,
            "model_badge": model_badge(model),
            "is_local": bool(is_local),
            "rotation": rotation,
            "channel_rpd": channel_rpd,
            "token_limit": tokens_limit,
            "tokens_used": tokens_used,
            "tokens_remaining": tokens_remaining,
            "reset_time": reset_time,
            "reset_epoch": reset_epoch,
            "twitch_connected": bool(live.get("twitch_connected")),
            "twitch_live": twitch_live,
            "ears_state": ears_state,
            "tts_engine": getattr(config, "TTS_ENGINE", None),
            "voice_mute": is_voice_muted(),
            "chat_mutes": chat_mute_snapshot(),
            "twitch_live_tracking": bool(twitch_live_tracking_enabled()),
            "wake_words": wake_words,
            "ooc": (ooc.snapshot() if ooc is not None else None),
            "discord_connected": discord_connected,
            "obs_enabled": obs_enabled,
            "kick_enabled": kick_enabled,
            "latest_transmission": transmission,
            "activity": activity,
            "latency": latency,
            "uptime_seconds": int(time.time() - self._started),
        }

    def write_now(self):
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        try:
            snap = self._snapshot()
            payload = json.dumps(snap, indent=2)
        except Exception as e:
            print(f"[red]Status feed serialise failed: {e}")
            return
        # The writer loop ticks twice a second, so with 500-char activity entries
        # this rewrite reached ~50KB twice a second -- and this repo lives in
        # OneDrive, which watches the file for changes. Compare everything except
        # uptime_seconds, which changes on every tick and would defeat the check.
        # A slow heartbeat rewrite still lands so the dashboard's uptime display
        # does not freeze while nothing else is happening.
        key = json.dumps({k: v for k, v in snap.items()
                          if k != "uptime_seconds"}, indent=2)
        now = time.time()
        if (key == self._last_payload
                and now - self._last_write < self.heartbeat_s):
            return
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
        except Exception as e:
            print(f"[red]Status feed serialise failed: {e}")
            return
        # os.replace() can't overwrite a file another process has open (Windows).
        # The dashboard reads in short bursts every 0.5s, so a missed snap is
        # harmless — retry briefly rather than dying.
        for _ in range(5):
            try:
                os.replace(tmp, self.path)
                self._last_payload = key
                self._last_write = now
                return
            except OSError:
                time.sleep(0.02)
        try:
            os.remove(tmp)
        except OSError:
            pass

    def start(self):
        if self._running:
            return
        self._running = True

        def _loop():
            while self._running:
                self.write_now()
                time.sleep(self.interval)

        self._writer = threading.Thread(target=_loop, daemon=True)
        self._writer.start()

    def stop(self):
        self._running = False


STATUS_FEED = StatusFeed()
