"""Playwright-backed fetcher for MediaWiki APIs hidden behind Cloudflare.

Some wikis (for example, ones behind a Cloudflare managed challenge) serve a
Turnstile challenge to any non-browser fingerprint: plain requests, curl_cffi
and cloudscraper all get a 403 "Just a moment". A real (headed) browser clears
the challenge and the trusted cf_clearance cookie persists in a local
Edge/Chrome profile, so subsequent runs reuse it and skip the verification.

The fetcher only launches the browser lazily, on the first Cloudflare 403,
and serves every API call through page.evaluate(fetch) so requests carry the
browser's own TLS fingerprint + cookies. Plain wikis (no challenge) never
touch this module.
"""

import json
import os
from urllib.parse import urlparse
from rich import print  # noqa: E402


class BrowserFetch:
    def __init__(self, profile_dir, channel="msedge", wait_seconds=90, timeout_ms=28800):
        self._profile_dir = profile_dir
        self._channel = channel
        self._wait_seconds = wait_seconds
        self._timeout_ms = timeout_ms
        self._pw = None
        self._ctx = None
        self._page = None
        self._ready_host = None
        self._channel_used = None

    def _browser_gone(self, err):
        """True when a Playwright error means the browser/driver died, not just a hiccup."""
        text = str(err).lower()
        return any(
            marker in text
            for marker in (
                "target closed",
                "browser has been closed",
                "connection closed",
                "browser closed",
                "has been closed",
                "websocket error",
                "disconnected",
                "econnreset",
                "no active websocket",
            )
        )

    def _restart_browser(self):
        """Tear down a wedged browser/driver and start a fresh one.

        A killed Edge can leave the persistent profile lock in a dirty state
        for a moment, so retry the relaunch a few times before giving up. Only
        the browser context is torn down here; the driver stays up because
        restarting sync_playwright() on the same thread can wedge.
        """
        import time

        self._close_context()
        last = None
        for attempt in range(4):
            time.sleep(1.0 + attempt * 2.0)
            try:
                self._start()
                return
            except Exception as e:  # noqa: BLE001 - retry, then surface
                last = e
        self._ready_host = None
        raise RuntimeError(f"Could not relaunch browser after 4 attempts: {last}") from last

    # ---------------------------------------------------------------- startup

    def _start(self):
        from playwright.sync_api import sync_playwright

        os.makedirs(self._profile_dir, exist_ok=True)
        if self._pw is None:
            self._pw = sync_playwright().start()
        channels = [self._channel] + ([c for c in ("chrome", "chromium") if c != self._channel])
        errors = []
        for chan in channels:
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(
                    self._profile_dir,
                    channel=None if chan == "chromium" else chan,
                    headless=False,
                    ignore_default_args=["--enable-automation"],
                    args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
                    viewport=None,
                )
                self._channel_used = chan
                break
            except Exception as e:  # noqa: BLE001 - try next channel
                errors.append(f"{chan}: {e}")
        if self._ctx is None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
            raise RuntimeError("Could not launch any browser: " + "; ".join(errors))
        self._ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        # page.evaluate() has no per-call timeout kwarg in the sync API, so the
        # guard is the in-page AbortController plus this default for RPC calls.
        self._ctx.set_default_timeout(self._timeout_ms + 5000)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        print(f"[dim]Browser session started ({self._channel_used}), waiting for Cloudflare check to clear...")

    def _ensure_ready(self, api_url):
        host = urlparse(api_url).netloc
        if self._ready_host == host:
            return
        if self._page is None:
            self._start()
        self._clear_challenge(host)
        self._ready_host = host
        print(f"[green]Cloudflare check cleared — API calls now proxied via browser ({host})")

    def _clear_challenge(self, host):
        """Load a real page until the interstitial ('Just a moment') is gone.

        Only the document title is trusted: Cloudflare injects
        'cdn-cgi/challenge-platform' into every page it serves, so sniffing
        the HTML for challenge markers false-positives on cleared pages.
        """
        import time

        for target in (f"https://{host}/wiki/Main_Page", f"https://{host}/"):
            try:
                self._page.goto(target, timeout=60000, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001 - keep polling; the tab may still commit
                pass
            deadline = time.time() + self._wait_seconds
            while time.time() < deadline:
                if self._loaded_and_clear():
                    return
                try:
                    self._page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001
                    pass
        raise RuntimeError(
            f"Cloudflare 'Just a moment' did not auto-clear within {self._wait_seconds}s "
            f"for {host}. Open https://{host}/ in a browser once, then retry."
        )

    def _loaded_and_clear(self):
        """True when the page finished a navigation and is not the interstitial."""
        try:
            state = self._page.evaluate("() => document.readyState")
            title = self._page.title() or ""
        except Exception:  # noqa: BLE001 - context destroyed mid-navigation
            return False
        if state not in ("interactive", "complete"):
            return False
        return bool(title.strip()) and "just a moment" not in title.lower()

    def is_ready(self, api_url):
        """True once a browser session is already cleared for this API host."""
        return self._ready_host is not None and self._ready_host == urlparse(api_url).netloc

    # ------------------------------------------------------------------ fetch

    def get_json(self, api_url, params):
        import time

        last_error = None
        for attempt in range(4):
            self._ensure_ready(api_url)
            try:
                result = self._page.evaluate(
                    """async (payload) => {
                        const url = payload.url;
                        const qs = new URLSearchParams(payload.params).toString();
                        const ctrl = new AbortController();
                        const timer = setTimeout(() => ctrl.abort(), payload.timeoutMs);
                        try {
                            const r = await fetch(url + '?' + qs, {
                                credentials: 'include', signal: ctrl.signal});
                            const body = await r.text();
                            return {status: r.status, body: body};
                        } finally {
                            clearTimeout(timer);
                        }
                    }""",
                    {"url": api_url, "params": {**params, "format": "json"},
                     "timeoutMs": self._timeout_ms},
                )
            except Exception as e:  # noqa: BLE001 - page navigated, timed out, or browser died
                last_error = e
                self._ready_host = None
                if self._browser_gone(e):
                    print("[yellow]Browser session went away — relaunching...")
                    self._restart_browser()
                time.sleep(1.5)
                continue
            if result.get("status") == 200:
                try:
                    return json.loads(result["body"])
                except ValueError as e:
                    last_error = e
                    break
            if result.get("status") in (403, 503):
                # Clearance expired or was not applied yet: re-clear and retry.
                last_error = RuntimeError(
                    f"Wiki API via browser returned HTTP {result.get('status')}: "
                    f"{(result.get('body') or '')[:120]}"
                )
                self._ready_host = None
                time.sleep(2)
                continue
            raise RuntimeError(
                f"Wiki API via browser returned HTTP {result.get('status')}: {(result.get('body') or '')[:120]}"
            )
        raise RuntimeError(f"Wiki API via browser failed after retries: {last_error}")

    # ---------------------------------------------------------------- teardown

    def _close_context(self):
        """Close only the browser context, keeping the Playwright driver alive.

        Restarting a dead browser is far cheaper and far more reliable when the
        driver stays up: tearing down sync_playwright() and starting a new one
        on the same thread can leave the greenlet loop wedged.
        """
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:  # noqa: BLE001 - it is already gone or wedged
            pass
        self._ctx = None
        self._page = None
        self._ready_host = None
        self._channel_used = None

    def close(self):
        self._close_context()
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = None