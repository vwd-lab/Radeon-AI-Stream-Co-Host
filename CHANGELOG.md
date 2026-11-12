# Changelog — Radeon AI Co-Host (share copy)

Canonical working copy at `C:\XXXXX\AI Projects\Project AI Logan\sharing\Radeon AI Co-Host`.

This is not a git repository, so versions are tracked here by date instead of commit hash.

## Unreleased

- Removed internet search integration (Google Custom Search / `needs_web_search` / `fetch_google_search_intel` / SQLite search cache) from `core/intel_pipeline.py` and `core/gemini_rest.py` — it did not work correctly while running on the local Ollama core. The bot now answers from its knowledge base and persona only.
- Added `setup_gui.py` + `setup_gui.bat` — tkinter setup GUI that lets you set `BOT_NAME` / `STREAMER_NAME` and related Twitch identity fields and write them to `.env` without editing the file by hand.
- Added **`SETUP · BOT IDENTITY`** pill to the LCARS dashboard (`dashboard.py`) that launches `setup_gui.py` as a detached process.
- `dashboard.py` reads `.env` identity values into its window title and header rail.

## 2026-10-26 — Share copy + pocket-only cleanup (main)

- Created share copy at `sharing\Radeon AI Co-Host` (Twitch + Pocket TTS only; Discord/Kick/OBS stripped).
- Renamed `logan_dashboard*` → `dashboard*`, `core/logan_facts.py` → `core/bot_facts.py`, `core/logan_intel_pipeline.py` → `core/intel_pipeline.py`.
- Genericized identity: `BOT_NAME` / `STREAMER_NAME` / `STREAMER_ALIASES`; dropped Logan/Kage/Admiral branding.
- `TOKEN_LIMIT` 3400 → 5000; `CHAT_HISTORY_TURNS` 4 → 12.
- Pocket TTS as sole engine; `tts/xtts_tts.py` deleted; shared helpers moved to `tts/base_tts.py`.
- Removed Discord, Kick, OBS WebSocket integrations and the OBS-driven PNGTuber avatar (`output/avatar_director.py`).
- Removed `avatar` smug hook and `on_ambient` callbacks.
- Internal data paths genericized: `data/status.json`, `data/cmd.json`, `data/bot.pid`, `data/memory.db`, `data/search_cache.db`.
- `_print_review_alerts()` stubbed out; `REVIEW_ALERT_DATE` dropped.
- `WIKI_BATCHES` replaced with generic `example` batch; `WIKI_BASE_URL` defaults to Wikipedia; `KB_RELEVANCE_FLOOR` default 0.20.
- `.env`, `.env.example`, `requirements.txt`, `.gitignore`, `README.md`, `COMMANDS.md` written for the share copy.

## 2026-10-24 — Personal facts + wiki backfill

- Personal facts deployed to main; wiki backfill (102 pages); `!wikirefresh` added.
- Main atexit VRAM-release port confirmed (`unload_all_llms` + `unload_all` on all three engine classes).

## 2026-10-23 — Pocket TTS as sole engine; batching fix

- `TTS_MAX_CHARS` 220 → 1000; chunked enqueue + single wait (2.81s vs 4.00s serial).
- `CHAT_BATCH_SIZE=2`, `CHAT_FLUSH_SECONDS=20` chat batching fix, verified.
- All three `.bat` files CRLF-converted (LF broke `goto :stop`).

## 2026-10-22 — Entity lore + knowledge base

- `core/viewer_lore.py` — `ViewerLoreManager`; source of truth `data/viewer_lore.json`, mirrored into SQLite `chatter_memory` table.
- `get_chatter_context()` / `get_chatter_contexts_multi()` inject entity lore into prompts.
- Kill switches `start_logan.bat stop` and `start_pocket_tts.bat stop`; PID file `data/logan.pid`.

## 2026-10-21 — ROADMAP / MAIN_BRANCH marker

- `ROADMAP.md` ("Road to Version 1") written with status legend.
- `MAIN_BRANCH.md` marker established for branch layout.

## 2026-10-20 — .env / config baseline

- `.env` and `.env.example` baseline committed.

## Pre-2026-10-20 — Project inception

- Initial `Radeon streamer-ai-voice` working copy (Logan Omega persona, Twitch + Kick + Discord + OBS, XTTS + Pocket TTS, local Ollama gemma4, knowledge base training).
