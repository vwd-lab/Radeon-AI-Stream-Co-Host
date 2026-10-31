import os
import json

from core.rate_limiter import throttle
from rich import print

import config

# Imported for their behaviour, not to reuse a class. `_resolve_think` decides
# whether this particular message should spend tokens on reasoning, and
# `_degenerate_reply` catches the stub replies that comes with. Both lived only
# in LocalAiEngine, which the show's pipeline bypasses entirely.
from core.ai_engine import _degenerate_reply, _resolve_think

# -------------------------------------------------------------
# 1. UNIFIED BRAIN FUNCTION
# -------------------------------------------------------------
async def generate_bot_response(
    user_prompt: str,
    username: str = "Chatter",
    provider: str = "gemini",
    system_prompt: str = "You are Bot Name, an authoritative stream assistant.",
    model_name: str | None = None,
    limiter=None,
    history: list | None = None,
) -> str:
    """
    Main pipeline handler.

    model_name optionally overrides the Gemini model so the caller can rotate
    across Ollama/dual-engine channels on quota errors, mirroring the old
    AiEngine channel rotation.

    limiter: optional callable used instead of the module-level throttle().
    A multi-bot show passes each co-host its own RateLimiter so one bot's
    burst does not silently pace the other two. Single-bot callers omit it and
    get the shared default exactly as before.
    """
    pace = limiter if limiter is not None else throttle

    # 2. ROUTE A: GEMINI (REST via httpx -- the genai SDK's transport stalls on
    # TLS to Google on this box, but plain httpx is reliable)
    if provider.lower() == "gemini":
        from core.gemini_rest import generate_text as gemini_generate

        if not os.getenv("GEMINI_API_KEY") and not getattr(config, "GEMINI_API_KEY", None):
            return "I don't have my Gemini key configured, so I can't answer that right now."

        model_name = model_name or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

        full_msg = f"Incoming comms from {username}: {user_prompt}"
        print(f"[yellow]\nAsking Gemini a question (channel: {model_name})...")
        pace()
        response = gemini_generate(
            model_name,
            full_msg,
            system_prompt=system_prompt if system_prompt else None,
        )
        return (response.text or "").strip()

    # 3. ROUTE B: GEMMA (Local via Ollama HTTP, mirroring LocalAiEngine)
    elif provider.lower() == "local":
        ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        model_name = os.getenv("OLLAMA_MODEL", "gemma3:4b")
        user_content = f"Incoming comms from {username}: {user_prompt}"

        # Resolved from the same helpers LocalAiEngine uses, so the two local
        # paths cannot disagree about what OLLAMA_THINK means.
        think = _resolve_think(user_prompt)

        # Prior turns first, so the local core follows the thread instead of
        # answering every message as if it were the first one in the stream.
        messages = [{"role": "system", "content": system_prompt}]
        for m in history or []:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": user_content})

        payload = json.dumps({
            "model": model_name,
            "messages": messages,
            "stream": False,
            # keep_alive matters more than it looks: without it Ollama may evict
            # the model between requests, and the next turn pays a cold load
            # measured in seconds rather than milliseconds. num_ctx matches
            # LocalAiEngine so both local paths behave identically.
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "options": {"num_ctx": max(config.OLLAMA_NUM_CTX, 4096)},
            # config.OLLAMA_THINK resolved per-message, so `auto` only spends
            # tokens reasoning on a technical question. Omitted entirely when
            # the setting is unrecognised, which leaves Ollama's model default.
            **({"think": think} if think is not None else {}),
        }).encode("utf-8")

        print(f"[yellow]\nAsking {model_name} (local)...")
        if think:
            print(f"[dim]  reasoning on ({config.OLLAMA_THINK}: technical message)")

        def _post(body: bytes):
            import urllib.request
            req = urllib.request.Request(
                f"{ollama_host}/api/chat",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            data = _post(payload)
        except Exception as e:
            print(f"[red]Ollama request failed: {e}")
            return ""

        answer = ((data.get("message", {}) or {}).get("content", "") or "").strip()

        # Local models can burn their token budget on reasoning and surface a
        # stub like "ok" or "ooc". LocalAiEngine already guards this; the show calls
        # this pipeline instead, so without the guard it had none.
        if _degenerate_reply(answer):
            print("[yellow]Local core returned a degenerate reply. "
                  "Retrying with a firm answer instruction...")
            retry = json.loads(payload.decode("utf-8"))
            retry["messages"][-1]["content"] += (
                "\n\n(Answer fully in character in complete sentences. "
                "Do not echo a single word from the prompt.)"
            )
            if retry.get("think"):
                # Retrying without reasoning beats repeating the same mistake.
                retry["think"] = False
                print("[dim]  retrying with reasoning off (stub under think=on)")
            try:
                data = _post(json.dumps(retry).encode("utf-8"))
                answer = ((data.get("message", {}) or {}).get("content", "")
                          or "").strip()
            except Exception as e:
                print(f"[red]Ollama retry failed: {e}")

        return answer

    else:
        raise ValueError(f"Unknown provider: {provider}")
