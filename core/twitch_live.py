"""Twitch Helix is-live check for the status dashboard.

Queries the public Twitch API (Helix) to answer one question: is the
streamer's channel actually streaming right now?

Result lives in a module-level cached value updated on a background thread,
so callers (the status feed's 0.5s writer) can read it without ever blocking
on the network.

Values: True / False / None (None = unknown: no creds yet, or a transient
API failure). A hiccup never reads as a false "live".

Auth: needs TWITCH_CLIENT_ID (bot app) plus one of these (both read from
config):

  * TWITCH_HELIX_TOKEN  - an explicit Helix access token issued BY that app.
                          (An IRC chat token often does NOT match the Helix
                          Client-ID -> 401; keep them separate.)
  * TWITCH_CLIENT_SECRET - the app's client secret; we auto-mint an app access
                           token via grant_type=client_credentials (~51 day
                           expiry, refreshed on demand). Public Helix endpoints
                           accept app tokens, so no user OAuth is needed.

The IRC chat token (TWITCH_OAUTH_TOKEN) is intentionally NOT used here - app
tokens can't join chat and chat tokens routinely mismatch the Helix app.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

import config
from rich import print  # noqa: E402

_BASE = "https://api.twitch.tv/helix"
_ID_BASE = "https://id.twitch.tv/oauth2/token"
_POLL_INTERVAL = 60.0
_HELIX_TOKEN_EXPIRE = 4_000_000.0  # refresh well before Twitch's ~51d app-token TTL

_lock = threading.Lock()
_twitch_live = None  # cached live status (True/False/None)
_enabled = True      # manual override: stop the poller (dashboard toggle)
_last_checked = 0.0
_worker = None
_helix_token = None
_helix_token_fetched = 0.0


def _http_json(url, headers, timeout=8):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_form(url, form, timeout=8):
    """POST a form-encoded body.

    Twitch's /oauth2/token endpoint only answers POST. A GET to the same URL
    returns 404, which is what happened when _refresh_token borrowed the GET
    helper above.
    """
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _headers_for(token):
    return {
        "Client-Id": config.TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }


def _refresh_token():
    """Mint a fresh Twitch app access token via client_credentials."""
    global _helix_token, _helix_token_fetched
    secret = config.TWITCH_CLIENT_SECRET
    if not config.TWITCH_CLIENT_ID or not secret:
        return None
    url = _ID_BASE
    form = {
        "client_id": config.TWITCH_CLIENT_ID,
        "client_secret": secret,
        "grant_type": "client_credentials",
    }
    try:
        data = _http_post_form(url, form, timeout=8)
    except Exception as e:
        print(f"[red]Twitch app token request failed: {e}")
        return None
    token = (data or {}).get("access_token")
    if not token:
        print("[red]Twitch app token response missing access_token")
        return None
    _helix_token = token
    _helix_token_fetched = time.time()
    print("[cyan]Minted a fresh Twitch app token for the live-check.")
    return token


def _current_token(force=False):
    """Helix bearer token: explicit TWITCH_HELIX_TOKEN, else auto-minted app token."""
    if config.TWITCH_HELIX_TOKEN:
        return config.TWITCH_HELIX_TOKEN
    stale = (time.time() - _helix_token_fetched) > _HELIX_TOKEN_EXPIRE
    if force or stale or not _helix_token:
        return _refresh_token()
    return _helix_token


def _user_id(login, token):
    data = _http_json(f"{_BASE}/users?login={quote(login)}", _headers_for(token))
    return (data.get("data") or [{}])[0].get("id")


def _streams_check(uid, token):
    data = _http_json(f"{_BASE}/streams?user_id={uid}", _headers_for(token))
    return bool(data.get("data"))


def _check_once():
    """One Helix round-trip; returns True/False/None."""
    login = config.TWITCH_LIVE_CHANNEL
    if not config.TWITCH_CLIENT_ID or not login:
        return None
    token = _current_token()
    if not token:
        return None
    try:
        uid = _user_id(login, token)
        if not uid:
            return None
        return _streams_check(uid, token)
    except urllib.error.HTTPError as e:
        if e.code == 401 and not config.TWITCH_HELIX_TOKEN:
            refreshed = _current_token(force=True)
            if refreshed:
                print("[cyan]Twitch token accepted after refresh.")
                try:
                    uid = _user_id(login, refreshed)
                    if not uid:
                        return None
                    return _streams_check(uid, refreshed)
                except urllib.error.HTTPError:
                    return None
                except Exception:
                    return None
        if e.code in (401, 400):
            print(f"[yellow]Twitch live check rejected ({e.code}) — check Client-ID/token")
        return None
    except Exception:
        return None


def _worker_loop():
    global _twitch_live, _last_checked
    while True:
        with _lock:
            on = _enabled
        value = None if not on else _check_once()
        with _lock:
            _twitch_live = value
            _last_checked = time.time()
        time.sleep(_POLL_INTERVAL)


def start(interval=_POLL_INTERVAL):
    """Start the background poller once. Safe to call repeatedly."""
    global _POLL_INTERVAL, _worker
    _POLL_INTERVAL = max(10, interval)
    if _worker and _worker.is_alive():
        return
    _worker = threading.Thread(target=_worker_loop, daemon=True)
    _worker.start()


def is_live():
    """Non-blocking. True/False if live, None if unknown or poller disabled."""
    with _lock:
        return None if not _enabled else _twitch_live


def is_enabled():
    """True when the background live poller is running (not manually off)."""
    with _lock:
        return _enabled


def set_enabled(on):
    """Manually enable/disable the live poller from the dashboard toggle.
    Disabling also clears any cached live status so nothing stale reports."""
    global _enabled, _twitch_live
    with _lock:
        _enabled = bool(on)
        if not _enabled:
            _twitch_live = None