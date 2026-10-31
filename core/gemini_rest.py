"""Plain-httpx generateContent client.

The google-genai SDK (2.19.0) fails on this box: its httpx transport stalls on
TLS handshakes to generativelanguage.googleapis.com (ConnectTimeout) while
plain httpx/curl complete the same connections in <1s and return 200. So all
Gemini traffic goes through direct REST calls here instead of the SDK.

Errors raise google.genai.errors.APIError (synthetic) so the shared
core.rate_limiter.classify_gemini_error() mapping -- rate_limit / quota /
overload / bad_model -- keeps working unchanged.
"""

import httpx
from types import SimpleNamespace

import config
from google.genai import errors as genai_errors

_BASE = "https://generativelanguage.googleapis.com/v1beta"
_API_KEY = ""


def _get_api_key():
    global _API_KEY
    if not _API_KEY:
        import os
        _API_KEY = os.environ.get("GEMINI_API_KEY") or getattr(config, "GEMINI_API_KEY", "") or ""
    return _API_KEY


def generate_text(
    model,
    contents,
    system_prompt=None,
    timeout=None,
):
    """POST a generateContent call over httpx and return the response text.

    Keyword args mirror the google-genai semantics used by the bot:
      - system_prompt -> systemInstruction
    API/transport errors are raised; the caller's classify/rotate logic (which
    catches google.genai.errors.APIError AND httpx transport errors) handles
    retries and channel rotation.
    """
    key = _get_api_key()
    url = f"{_BASE}/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": contents}]}]}
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    deadline = timeout or config.GEMINI_REQUEST_TIMEOUT_S or 45
    resp = httpx.post(
        url,
        params={"key": key} if key else None,
        json=body,
        timeout=deadline,
        headers={"x-goog-api-client": "ai-cohost"},
    )

    if resp.status_code >= 400:
        err_json = None
        try:
            err_json = resp.json()
        except Exception:
            err_json = {
                "error": {
                    "code": resp.status_code,
                    "message": resp.text[:300] or str(resp.status_code),
                    "status": "",
                }
            }
        raise genai_errors.APIError(resp.status_code, err_json, resp)

    data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError) as e:
        raise genai_errors.APIError(
            502,
            {"error": {"code": 502, "message": f"Malformed response: {data}", "status": "INTERNAL"}},
        ) from e

    usage = None
    meta = data.get("usageMetadata")
    if isinstance(meta, dict):
        usage = SimpleNamespace(
            prompt_token_count=meta.get("promptTokenCount") or 0,
            candidates_token_count=meta.get("candidatesTokenCount") or 0,
            total_token_count=meta.get("totalTokenCount") or 0,
        )
    return SimpleNamespace(text=text, usage_metadata=usage)