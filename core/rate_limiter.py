"""Global throttling + 429-classification helpers for Gemini API calls.

The Gemini API enforces per-minute rate limits (RPM) and per-minute token
limits (TPM) separately from the daily request cap. Free tiers are roughly
5-15 RPM, so every Gemini call is paced through throttle() so reply bursts
can't trip the burst bucket. classify_gemini_error() tells callers whether a
429 is a rate-limit (hold & retry the same channel) or a quota/overload
(rotate to the next channel).
"""

import random
import threading
import time

import config
from google.genai import errors as genai_errors
from rich import print


_lock = threading.Lock()
_last_call_ts = 0.0
_last_wait_notice_ts = 0.0


def throttle():
    """Block until enough time has passed since the last Gemini API call.

    Serializes all Gemini traffic from every caller (brain replies, !deepmine
    batches, fact extraction) so bursts can't trip the per-minute bucket.
    Prints a pacing notice (cooldowned) so a queued call never looks frozen.

    Module-level state rather than a RateLimiter instance: a multi-bot show
    would give each co-host its own pacing state, but nothing here constructs
    one yet. Split this out when that happens instead of pre-building it.
    """
    global _last_call_ts, _last_wait_notice_ts
    while True:
        with _lock:
            now = time.monotonic()
            wait = config.GEMINI_MIN_INTERVAL_S - (now - _last_call_ts)
            if wait <= 0:
                _last_call_ts = now
                return
        if wait > 1.0 and now - _last_wait_notice_ts > 30.0:
            _last_wait_notice_ts = now
            print(f"[dim]Pacing Gemini calls (rate limit) — waiting {wait:.1f}s...")
        time.sleep(min(wait, 0.25))


def backoff_delay(attempt):
    """Exponential backoff with jitter: base*2^(attempt-1) + jitter, capped."""
    exp = config.GEMINI_BACKOFF_BASE_S * (2 ** (attempt - 1))
    jitter = random.uniform(0, exp * 0.25)
    return min(exp + jitter, config.GEMINI_MAX_BACKOFF_S)


def _reason_from_error(e):
    """Pull the machine reason (RATE_LIMIT_EXCEEDED / QUOTA_EXCEEDED / ...)."""
    details = getattr(e, "details", None)
    if not isinstance(details, dict):
        return None
    error = details.get("error") or details
    if isinstance(error, dict):
        for d in error.get("details") or []:
            if isinstance(d, dict) and d.get("reason"):
                return str(d["reason"])
        status = error.get("status")
        if status:
            return str(status)
    return None


def is_transport_error(e):
    """True for network/TLS/socket timeouts (the kind of blip worth retrying
    the same channel for) as opposed to a real API error response."""
    if isinstance(e, (TimeoutError, OSError)):
        return True
    mod = (getattr(e, "__class__", None) and
           getattr(e.__class__, "__module__", "") or "")
    return mod.startswith("httpx") or mod.startswith("httpcore")


def classify_gemini_error(e):
    """Classify an exception from a Gemini call for retry handling.

    Returns one of:
      'rate_limit' - per-minute RPM/TPM bucket hit; hold this channel and
                     retry after backoff (rotating won't escape a project pool).
      'quota'      - daily RPD exhausted; rotate to the next channel.
      'overload'   - 503-ish high demand OR a transient network/timeout; rotate
                     like a quota error.
      'bad_model'  - 404 NOT_FOUND: the model name isn't served on this account
                     right now (Google rotates names); skip & rotate.
      None         - not retryable (or not a google.genai API error).
    """
    if not isinstance(e, genai_errors.APIError):
        # Network / TLS / transport timeouts (httpx.ConnectTimeout,
        # httpcore.ConnectTimeout, httpx.ReadTimeout, ...) are transient —
        # rotate to the next channel rather than crashing the bot on a flaky
        # connection. Detect by module to avoid a hard dependency, and fall
        # back to OSError/TimeoutError for other socket failures.
        if is_transport_error(e):
            return "overload"
        return None
    code = str(getattr(e, "code", None) or "")
    status = getattr(e, "status", None) or ""
    reason = (_reason_from_error(e) or "").upper()
    msg_lower = (str(e) or "").lower()

    if "RATE_LIMIT" in reason or "RATELIMIT" in reason:
        return "rate_limit"
    if "QUOTA" in reason:
        return "quota"
    if code == "429" or status == "RESOURCE_EXHAUSTED":
        # Ambiguous resource exhaustion defaults to quota (rotate) for safety,
        # unless the message betrays a per-minute limit.
        if "per minute" in msg_lower or "rate limit" in msg_lower:
            return "rate_limit"
        return "quota"
    try:
        code_int = int(code)
    except ValueError:
        code_int = 0
    if status == "UNAVAILABLE" or code_int >= 500:
        return "overload"
    if code_int == 404 or status == "NOT_FOUND":
        return "bad_model"
    if code_int and code_int < 500:
        # 4xx client errors are the bot's own bad request (bad payload, model,
        # key, safety/etc.) — NOT server overload. Never label them 503 or burn
        # channel rotations on them; surface the true status instead. (A 400
        # whose text happens to say "unavailable" must not be classed overload.)
        return None
    if "unavailable" in msg_lower or "high demand" in msg_lower:
        return "overload"
    return None


def channel_rpd_left(model_name, memory_db):
    """Requests still available today before a channel hits its hard-coded RPD
    cap. None when gating is off, the channel has no cap, or usage can't be
    tracked (caller then assumes the channel is not RPD-capped)."""
    if not config.GEMINI_RPD_ENABLED:
        return None
    cap = config.CHANNEL_RPD.get(model_name)
    if not cap:
        return None
    if memory_db is None:
        return None
    get = getattr(memory_db, "get_channel_usage", None)
    if not get:
        return None
    used = get(model_name)
    return cap - used


def channel_rpd_exhausted(model_name, memory_db):
    """True when a channel is at/past its daily RPD cap (predictive 429 gate)."""
    left = channel_rpd_left(model_name, memory_db)
    return left is not None and left <= 0