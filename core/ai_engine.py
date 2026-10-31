import os
import json
import time
import random
import threading
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from google.genai import errors
from rich import print

from core.gemini_rest import generate_text as gemini_generate

from core.rate_limiter import (
    backoff_delay,
    channel_rpd_exhausted,
    classify_gemini_error,
    is_transport_error,
    throttle,
)


SLEEP_LINES = [
    ("Europe/Athens", "I'm tired, I'm going to hit the sack. I'll be in my bunk until {reset} Eastern European Time."),
    ("Asia/Tokyo", "*yawns*, I'm going to hit the hay. I'll be in my rack until {reset} Japan Standard Time."),
    ("America/Anchorage", "I'm going to sleep. I'll be on my cot at the back of your office until {reset} Alaska Daylight Time."),
]

CLUTTERED_LINE = "Comms are getting cluttered up here! Give it 30 seconds and try again."

STREAMING_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]


# Words that mark a message as a factual/technical question rather than banter.
# Used by OLLAMA_THINK=auto to decide whether the local model should reason.
# Kept deliberately narrow: a false positive costs ~10-30x latency on reasoning models and
# risks the empty-reply failure mode, so only obvious technical vocabulary
# turns reasoning on.
_TECHNICAL_MARKERS = (
    "how do i", "how do you", "how to", "what's the best", "what is the best",
    "whats the best", "fastest way", "best way", "what should i", "any tips",
    "build", "gear", "reputation", "skill tree", "skilltrees", "stats",
    "specs", "spec", "level", "leveling", "grind", "queue", "difficulty",
    "which one", "worth it", "counter", "meta", "expansion", "reputation table",
    "stt", "sto", "ship", "starship", "console", "keybind", "cooldown",
    "damage", "crit", "haste", "recharge", "accuracy", "range",
)


def _wants_reasoning(text):
    """True when a message looks like a technical/factual question."""
    t = (text or "").lower()
    if not t:
        return False
    if any(m in t for m in _TECHNICAL_MARKERS):
        return True
    if "?" in t and any(t.startswith(p) for p in
                        ("what", "which", "how", "when", "where", "why", "can i")):
        return True
    return False


def _resolve_think(user_text):
    """Resolve config.OLLAMA_THINK into the `think` flag for a request.

    Returns True/False, or None when the setting is unrecognised (Ollama then
    applies the model default).
    """
    mode = getattr(config, "OLLAMA_THINK", "auto")
    if mode in ("off", "false", "no", "0"):
        return False
    if mode in ("on", "true", "yes", "1"):
        return True
    if mode == "auto":
        return _wants_reasoning(user_text)
    return None


def _degenerate_reply(text, relax=False):
    """True when a reply is empty or a one-token stub (thinking models often
    burn their budget on reasoning and surface a near-empty answer like 'ooc').

    With relax=True (used in OOC mode) only an empty/whitespace reply is
    degenerate: short plain answers like "Yeah." or "Aha." are legitimate
    out-of-character responses, so the <8-char and bare-"ooc" rules are dropped.
    """
    t = (text or "").strip().lower()
    if not t:
        return True
    if relax:
        return False
    if len(t) < 8:
        return True
    return t in {".", "...", "ok", "ok.", "n/a", "n/a.", "error", "error.",
                 "ready", "yes", "no", "ooc"}


def unload_all_llms(host=None, timeout=10):
    """Release every model currently resident in VRAM from Ollama.

    Asks /api/ps what is actually loaded rather than trusting config, so this
    also frees a model loaded outside the bot (a manual `ollama run`, or another
    branch). Runs on shutdown because Ollama holds a model for
    OLLAMA_KEEP_ALIVE (60m) after the bot exits, which would otherwise leave the
    GPU pinned once the program is closed.

    Returns (unloaded, errors) as lists of model names.
    """
    base = (host or config.OLLAMA_HOST).rstrip("/")
    unloaded, errors = [], []
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return [], [f"could not list models: {e}"]

    for m in data.get("models", []) or []:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        try:
            body = json.dumps({"model": name, "keep_alive": 0}).encode("utf-8")
            req = urllib.request.Request(
                f"{base}/api/generate", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read()
            unloaded.append(name)
        except Exception as e:
            errors.append(f"{name}: {e}")
    return unloaded, errors


class _FloodGate:
    def check_flood(self, source):
        if source != "chat":
            return None
        now = time.time()
        self._recent_interactions.append(now)
        while self._recent_interactions and now - self._recent_interactions[0] > 50:
            self._recent_interactions.popleft()
        if len(self._recent_interactions) <= 4:
            return None
        if now - self._last_complaint < 30:
            return ""
        self._last_complaint = now
        return CLUTTERED_LINE


class AiEngine(_FloodGate):
    def __init__(self, memory_db=None):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            exit("You forgot to set GEMINI_API_KEY in your environment!")
        self.memory_db = memory_db
        self.budget_shutdown_requested = False
        self.model_index = 0
        self._consecutive_429 = 0
        self.exhausted_models = set()  # Track 429 quota exhausted models for the session
        self._exhausted_date = None
        self._recent_interactions = deque()
        self._last_complaint = 0.0
        self._models_cache = self._discover_models()
        print(f"[green]Gemini model loaded: {self.current_model()} "
              f"(channels: {', '.join(self._models_cache)})")

    @property
    def _models(self):
        return self._models_cache

    def _discover_models(self):
        """Prefer the models this account actually serves right now.

        Google rotates these model names fast (older ones 404 with
        'no longer available... use X'), so a hard-coded list churns 404s
        and fake '503 high demand' rotates. Ask for the live generateContent
        set instead; fall back to STREAMING_MODELS if discovery itself fails.
        Discovery uses plain httpx (not the SDK client, whose transport has
        been observed to stall on TLS handshakes the raw stack handles fine).
        """
        key = os.environ.get("GEMINI_API_KEY")
        url = ("https://generativelanguage.googleapis.com/v1beta/models"
               if key is None else
               f"https://generativelanguage.googleapis.com/v1beta/models?key={key}")
        try:
            names = []
            for att in range(3):
                error = None
                try:
                    import httpx
                    resp = httpx.get(url, timeout=15,
                                     headers={"x-goog-api-client": "genai-discovery"})
                    resp.raise_for_status()
                    data = resp.json()
                    for m in data.get("models") or []:
                        methods = m.get("supportedGenerationMethods") or []
                        if "generateContent" in methods:
                            name = (m.get("name") or "").replace("models/", "", 1)
                            if name:
                                names.append(name)
                    error = None
                    break
                except Exception as e:
                    error = e
                    if att < 2:
                        print(f"[yellow]Gemini model discovery attempt {att + 1} failed "
                              f"({type(e).__name__}); retrying in {2 * (att + 1)}s...")
                        time.sleep(2 * (att + 1))
            if error is not None:
                raise error
            if names:
                # Only prune the configured channels to the live set. Never
                # append extra models: daily quota is per project (not per
                # channel), so surplus channels can't escape a 429 and just
                # turn an exhausted rotation into a slow grind. Legacy names
                # Google still advertises (gemini-2.5-*) 404 on generate, so
                # any pruned-out channel is a win. Skip models that aren't
                # general text chat (image/tts/etc.).
                skip = ("image", "tts", "transcribe", "omni", "lyria",
                        "nano-banana", "robotics", "computer-use",
                        "antigravity", "deep-research", "gemma")
                useful = [n for n in names
                          if not any(s in n for s in skip)]
                live = [n for n in STREAMING_MODELS if n in useful]
                if live:
                    return live
        except Exception as e:
            print(f"[yellow]Gemini model discovery failed "
                  f"({type(e).__name__}); using configured list.")
        return STREAMING_MODELS

    def current_model(self):
        return self._models[self.model_index % len(self._models)]

    def unload_all(self):
        """Free every resident model. Present on all engines so the
        shutdown hook works regardless of which one is active."""
        return unload_all_llms()

    def set_frequency(self, number):
        try:
            n = int(number)
        except (TypeError, ValueError):
            return False
        if 1 <= n <= len(self._models):
            self.model_index = n - 1
            self._consecutive_429 = 0
            return True
        return False

    def mark_exhausted(self, model_name):
        self.exhausted_models.add(model_name)

    def _refresh_exhausted(self):
        """Drop the session's exhausted-model set when the calendar day rolls.
        Google's daily quota resets at midnight, so channels stay dead until
        then, then come back on the new day."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._exhausted_date != today:
            self.exhausted_models.clear()
            self._exhausted_date = today

    def rotate_channel(self):
        start = self.model_index
        for _ in range(len(self._models)):
            self.model_index = (self.model_index + 1) % len(self._models)
            # If we cycled back to the start, all options in Gemini are exhausted
            if self.model_index == start:
                return False
            if self.current_model() not in self.exhausted_models:
                print(f"[cyan]Rotated comm channel -> {self.current_model()}")
                return True
        return False

    def _budget_exhausted_reply(self, reason):
        self.budget_shutdown_requested = True
        statement = self._sleep_statement()
        print(f"[red]{reason}")
        print(f"[green]\n{statement}\n")
        return statement

    def _remaining_tokens(self):
        if not self.memory_db:
            return None
        used = self.memory_db.get_tokens_used_today()
        return config.DAILY_TOKEN_LIMIT - used

    def _next_reset_time(self, tz_key):
        now = datetime.now().astimezone()
        reset_local = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return reset_local.astimezone(ZoneInfo(tz_key)).strftime("%I:%M %p").lstrip("0")

    def _next_reset_epoch(self, tz_key="Europe/Athens"):
        tz = ZoneInfo(tz_key)
        now_eet = datetime.now(tz)
        reset_eet = (now_eet + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return int(reset_eet.timestamp())

    def _sleep_statement(self):
        tz_key, template = random.choice(SLEEP_LINES)
        return template.format(reset=self._next_reset_time(tz_key))

    def _log_usage(self, response):
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return
        total = usage.total_token_count or 0
        used_today = self.memory_db.add_token_usage(total) if self.memory_db else None
        line = (
            f"[dim]Tokens — prompt: {usage.prompt_token_count}, "
            f"reply: {usage.candidates_token_count}, total: {total}"
        )
        if used_today is not None:
            remaining = max(0, config.DAILY_TOKEN_LIMIT - used_today)
            line += f" | today: {used_today}/{config.DAILY_TOKEN_LIMIT} (left: {remaining})"
        line += f" | channel: {self.current_model()}"
        print(line)

    def chat_single(self, prompt):
        print("[yellow]\nAsking Gemini a single question...")
        self._refresh_exhausted()
        total_attempts = 0
        rate_attempts = 0
        transport_retries = 0
        consecutive_quota = 0
        while total_attempts < len(self._models):
            total_attempts += 1
            current = self.current_model()
            
            # Skip channels that 429-quota'ed earlier this session OR are already
            # at their hard-coded daily RPD cap (predictive: don't burn a doomed
            # call; a rejected request still counts against the cap).
            if current in self.exhausted_models or (
                    channel_rpd_exhausted(current, self.memory_db)):
                self.mark_exhausted(current)
                if not self.rotate_channel():
                    break
                continue

            try:
                throttle()
                response = gemini_generate(current, prompt)
                break
            except Exception as e:
                code = getattr(e, "code", None) or ""
                message = str(e)
                kind = classify_gemini_error(e)
                # Don't swallow bugs in our own code (non-API, non-transport).
                if kind is None and not isinstance(e, errors.APIError) \
                        and not is_transport_error(e):
                    raise
                if kind is None:
                    print(f"[red]Gemini API error ({code}): {message}")
                    return None
                if kind == "rate_limit":
                    rate_attempts += 1
                    if rate_attempts >= config.GEMINI_RATE_RETRIES:
                        print(f"[yellow]{current} still rate-limited after {rate_attempts} "
                              "retries. Rotating to the next channel.")
                        self.mark_exhausted(current)
                        if not self.rotate_channel():
                            break
                        rate_attempts = 0
                    else:
                        delay = backoff_delay(rate_attempts)
                        print(f"[yellow]{current} rate-limited (RPM/TPM). Backing off "
                              f"{delay:.1f}s and retrying the same channel...")
                        time.sleep(delay)
                    continue
                # quota / overload -> rotate & mark as exhausted if quota
                rate_attempts = 0
                # dead model name (404) -> skip & rotate, don't abort the reply
                if kind == "bad_model":
                    print(f"[red]Channel {current} is not served (404) — skipping "
                          "it for the rest of this session.")
                    self.mark_exhausted(current)
                    transport_retries = 0
                    if not self.rotate_channel():
                        break
                    time.sleep(1)
                    continue
                # transient TLS/network blip -> retry the SAME channel before rotating
                if kind == "overload" and is_transport_error(e) and transport_retries < 2:
                    transport_retries += 1
                    delay = backoff_delay(transport_retries)
                    print(f"[yellow][transport] Gemini TLS/conn blip on {current} — "
                          f"retrying the same channel in {delay:.1f}s "
                          f"(retry {transport_retries}/2)...")
                    time.sleep(delay)
                    continue
                transport_retries = 0
                # Quota is per project, not per channel: if two channels in a
                # row quota-429 it's the daily cap, and rotating more can never
                # help. Stop grinding and report the exhaustion.
                if kind == "quota":
                    concurrent_quota = consecutive_quota + 1
                    consecutive_quota = concurrent_quota
                    if consecutive_quota >= 2:
                        print("[red]Gemini daily request quota exhausted "
                              "(project-wide) — every channel is 429. "
                              "Stopping rotation.")
                        return None
                    self.mark_exhausted(current)
                else:
                    consecutive_quota = 0
                reason = "under high demand (503)" if kind == "overload" else "out of quota (429)"
                print(f"[yellow]Channel {current} is {reason}. Rotating comm frequencies...")
                if not self.rotate_channel():
                    break
                time.sleep(10)
        else:
            print("[red]All comm channels failed (high demand or quota exhausted). Try again later.")
            return None
        
        if self.memory_db:
            self.memory_db.add_request_usage(1)
            if hasattr(self.memory_db, "add_channel_usage"):
                self.memory_db.add_channel_usage(current, 1)
        self._log_usage(response)
        return response.text


class LocalAiEngine(_FloodGate):
    def __init__(self, memory_db=None):
        self.memory_db = memory_db
        self.budget_shutdown_requested = False
        self.host = config.OLLAMA_HOST.rstrip("/")
        self.model_name = config.OLLAMA_MODEL
        self._recent_interactions = deque()
        self._last_complaint = 0.0
        print(f"[green]Local model loaded: {self.current_model()} @ {self.host} "
              f"(num_ctx={config.OLLAMA_NUM_CTX})")

    @property
    def _models(self):
        return [self.model_name]

    def current_model(self):
        return self.model_name

    def unload_all(self):
        return unload_all_llms()

    def _next_reset_time(self, tz_key):
        now = datetime.now().astimezone()
        reset_local = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return reset_local.astimezone(ZoneInfo(tz_key)).strftime("%I:%M %p").lstrip("0")

    def _next_reset_epoch(self, tz_key="Europe/Athens"):
        tz = ZoneInfo(tz_key)
        now_eet = datetime.now(tz)
        reset_eet = (now_eet + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return int(reset_eet.timestamp())

    def set_frequency(self, number):
        try:
            return int(number) == 1
        except (TypeError, ValueError):
            return False

    def _post(self, path, payload, timeout=None):
        url = f"{self.host}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or config.OLLAMA_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[red]Ollama request failed ({url}): {e}")
            return None

    def chat_single(self, prompt):
        print(f"[yellow]\nAsking {self.model_name} a single question (local)...")
        data = self._post("/api/chat", {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "options": {"num_ctx": max(config.OLLAMA_NUM_CTX, 4096)},
        })
        if data is None:
            print("[red]Local model unreachable. No answer.")
            return None
        answer = ((data.get("message") or {}).get("content") or "").strip() or None
        if answer:
            self._log_local_usage(data)
        return answer

    def chat(self, history, relax=False):
        """Full multi-turn chat call for the local core (used by
        _local_fallback when every Gemini channel is exhausted).

        relax=True (OOC mode) disables the short-answer degenerate retry: short
        plain replies are legitimate out-of-character responses."""
        if not history:
            print("[red]No messages provided for local chat!")
            return None

        system_instruction = None
        messages = []
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_instruction = content
            elif role in ("user", "assistant"):
                messages.append({"role": role, "content": content})

        merged = []
        for msg in messages:
            if merged and merged[-1]["role"] == msg["role"]:
                merged[-1]["content"] += "\n" + msg["content"]
            else:
                merged.append(dict(msg))

        if not merged:
            return None
        if system_instruction:
            merged.insert(0, {"role": "system", "content": system_instruction})

        # Local reasoning mode. On reasoning models, thinking costs ~9s versus 0.32s and
        # can return an empty answer, so it is opt-in per request here.
        user_text = ""
        for m in reversed(merged):
            if isinstance(m, dict) and m.get("role") == "user":
                user_text = m.get("content") or ""
                break
        think = _resolve_think(user_text)

        payload = {
            "model": self.model_name,
            "messages": merged,
            "stream": False,
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "options": {"num_ctx": max(config.OLLAMA_NUM_CTX, 4096)},
        }
        if think is not None:
            payload["think"] = think
        if think:
            print(f"[dim]  reasoning on ({config.OLLAMA_THINK}: technical message)")
        print(f"[yellow]\nAsking {self.model_name} (local fallback)...")
        data = self._post("/api/chat", payload)
        if data is None:
            print("[red]Local model unreachable. No answer.")
            return None
        answer = ((data.get("message") or {}).get("content") or "").strip() or None
        if answer:
            self._log_local_usage(data)

        if answer is None or _degenerate_reply(answer, relax=relax):
            print("[yellow]Local core returned a degenerate reply. "
                  "Retrying with a firm answer instruction...")
            retry_msgs = [dict(m) for m in merged]
            retry_msgs[-1] = dict(retry_msgs[-1])
            retry_msgs[-1]["content"] += (
                ("\n\n(Answer fully as the co-host in complete sentences. "
                 "Do not echo a single word from the prompt.)"
                 if not relax else
                 "\n\n(Answer in complete sentences. "
                 "Do not echo a single word from the prompt.)")
            )
            payload["messages"] = retry_msgs
            # Reasoning models can burn their token budget and return a stub,
            # so retrying without reasoning beats repeating the same mistake.
            if payload.get("think"):
                payload["think"] = False
                print("[dim]  retrying with reasoning off (empty reply under think=on)")
            data = self._post("/api/chat", payload)
            if data is None:
                print("[red]Local model unreachable on retry. No answer.")
                return None
            answer = ((data.get("message") or {}).get("content") or "").strip() or None
            if answer:
                self._log_local_usage(data)
        return answer

    def _log_local_usage(self, data):
        prompt_tok = data.get("prompt_eval_count") or 0
        reply_tok = data.get("eval_count") or 0
        total_s = (data.get("total_duration") or 0) / 1e9
        eval_s = (data.get("eval_duration") or 0) / 1e9
        tps = reply_tok / eval_s if eval_s else 0
        line = (f"[dim]Tokens — prompt: {prompt_tok}, reply: {reply_tok} | "
                f"gen: {total_s:.1f}s (~{tps:.1f} tok/s) | {self.current_model()} (local, no quota)")
        print(line)


class DualEngine:
    def __init__(self, memory_db=None, gemini_engine=None, local_engine=None):
        self.gemini = gemini_engine or AiEngine(memory_db=memory_db)
        self.local = local_engine or LocalAiEngine(memory_db=memory_db)
        self.memory_db = memory_db
        # Gemini is normally tried first and local is only a fallback once the
        # cloud channels are exhausted. LOCAL_FIRST flips that so the local
        # model serves from the first reply and Gemini absorbs the overflow.
        self._using_local = bool(getattr(config, "LOCAL_FIRST", False))

    @property
    def budget_shutdown_requested(self):
        return self.gemini.budget_shutdown_requested

    @property
    def _models(self):
        return list(self.gemini._models) + [f"{self.local.model_name} (local)"]

    def current_model(self):
        return self.local.current_model() if self._using_local else self.gemini.current_model()

    def unload_all(self):
        return unload_all_llms()

    def _next_reset_time(self, tz_key):
        return self.gemini._next_reset_time(tz_key)

    def _next_reset_epoch(self, tz_key="Europe/Athens"):
        return self.gemini._next_reset_epoch(tz_key)

    def check_flood(self, source):
        """Pass flood check requests to whichever engine is currently active."""
        return self.local.check_flood(source) if self._using_local else self.gemini.check_flood(source)

    def set_frequency(self, number):
        try:
            n = int(number)
        except (TypeError, ValueError):
            return False
        gemini_count = len(self.gemini._models)
        if 1 <= n <= gemini_count:
            leaving_local = self._using_local
            ok = self.gemini.set_frequency(n)
            if ok:
                self._using_local = False
                if leaving_local:
                    self._unload_local()
            return ok
        if n == gemini_count + 1:
            if not self._using_local:
                self._using_local = True
                print(f"[cyan]Dialed {self.local.current_model()} (local) - first reply may pause while the core loads.")
            return True
        return False

    def rotate_channel(self):
        """Rotate through Gemini models. If Gemini channels are exhausted, drop to local core."""
        if self._using_local:
            return False  # Already on local, nowhere else to rotate

        rotated = self.gemini.rotate_channel()
        if not rotated:
            # All Gemini channels exhausted -> automatically switch to local core
            self._using_local = True
            print(f"[yellow]All Gemini channels exhausted. Switched to local core: {self.local.current_model()}")
            return True
        return True

    def chat_single(self, prompt):
        if self._using_local:
            return self.local.chat_single(prompt)
        
        res = self.gemini.chat_single(prompt)
        if res is None:
            # Fallback to local if all Gemini attempts failed
            print("[yellow]Gemini API failed or quota exhausted — falling back to local core...")
            self._using_local = True
            return self.local.chat_single(prompt)
        return res

    def _unload_local(self):
        def _release():
            try:
                self.local._post("/api/generate", {
                    "model": self.local.model_name,
                    "keep_alive": 0,
                }, timeout=15)
                print("[cyan]Local core released (VRAM freed for the game).")
            except Exception as e:
                print(f"[yellow]Local core unload ping failed (harmless): {e}")
        threading.Thread(target=_release, daemon=True).start()