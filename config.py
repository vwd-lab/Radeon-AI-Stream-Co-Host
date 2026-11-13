import json
import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
# BOT_NAME is what the co-host calls itself. It is injected into the persona
# prompt, so changing it here is enough to rename the bot.
BOT_NAME = os.getenv("BOT_NAME", "Bot Name").strip() or "Bot Name"

# STREAMER_NAME is how the bot refers to the streamer (in prompts and replies).
# Keep it free of spaces and titles.
#
# Who counts as the streamer for owner-only commands is decided by WHERE a
# message comes from, not by name: the console, the microphone, the dashboard,
# and on Twitch the owner of TWITCH_CHANNEL (a channel's name is its owner's
# login). Nothing needs to be listed for that.
#
# STREAMER_ALIASES is optional: extra accounts that also get owner commands,
# such as an alt Twitch login, or a Kick account written as kick:yourkickname.
# It used to default to "streamer", which gave owner commands to any Twitch
# account called "streamer"; it now defaults to empty.
STREAMER_NAME = os.getenv("STREAMER_NAME", "Streamer").strip() or "Streamer"
STREAMER_TWITCH_NAME = os.getenv("STREAMER_TWITCH_NAME", "streamer").strip().lower()
STREAMER_ALIASES = {
    n.strip().lower() for n in os.getenv(
        "STREAMER_ALIASES", ""
    ).split(",") if n.strip()
}

# ---------------------------------------------------------------------------
# Twitch
# ---------------------------------------------------------------------------
TWITCH_OAUTH_TOKEN = os.getenv("TWITCH_OAUTH_TOKEN", "")
TWITCH_BOT_NICK = os.getenv("TWITCH_BOT_NICK", "BotName")
TWITCH_CHANNEL = os.getenv("TWITCH_CHANNEL", "")
TWITCH_MENTION_NAME = os.getenv("TWITCH_MENTION_NAME", BOT_NAME)
TWITCH_COMMAND_PREFIX = os.getenv("TWITCH_COMMAND_PREFIX", "!ask")

# Several bots can share one Twitch account, so each one prefixes its own name
# into the message body. Leave empty to post replies unprefixed.
TWITCH_REPLY_PREFIX = os.getenv("TWITCH_REPLY_PREFIX", BOT_NAME).strip()
# Post reply text into Twitch chat (separate from the spoken reply).
TWITCH_TEXT_REPLIES = os.getenv("TWITCH_TEXT_REPLIES", "0") == "1"

# Twitch live-stream check (Helix API). Client-ID from a Twitch dev app; the
# OAuth token above reads public /streams. The live light shows "unknown" until
# a client-id is set.
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_HELIX_TOKEN = os.getenv("TWITCH_HELIX_TOKEN", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_LIVE_CHANNEL = os.getenv("TWITCH_LIVE_CHANNEL", "").strip() or STREAMER_TWITCH_NAME

# ---------------------------------------------------------------------------
# Speech-to-text / audio
# ---------------------------------------------------------------------------
VOSK_MODEL_PATH = os.getenv(
    "VOSK_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models",
                 "vosk-model-small-en-us"))
MIC_DEVICE = os.getenv("MIC_DEVICE", "")
AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "")
WHISPER_MODEL = "base"

# ---------------------------------------------------------------------------
# Voice engine (Pocket TTS)
# ---------------------------------------------------------------------------
# Pocket speaks over HTTP to the server started by start_pocket_tts.bat, which
# runs in a separate CPU-torch venv.
TTS_ENGINE = os.getenv("TTS_ENGINE", "pocket").strip().lower()
POCKET_SERVER_URL = os.getenv("POCKET_SERVER_URL", "http://localhost:8123")
POCKET_TIMEOUT_S = float(os.getenv("POCKET_TIMEOUT_S", "60"))
# Optional Pocket generation knobs (read by tools/export_pocket_voice.py).
POCKET_LANGUAGE = os.getenv("POCKET_LANGUAGE", "english_2026-09").strip().lower()
POCKET_DECODE_STEPS = int(os.getenv("POCKET_DECODE_STEPS", "1"))

# ---------------------------------------------------------------------------
# Streaming tool
# ---------------------------------------------------------------------------
# none | streamlabs. OBS overlay is optional and off by default.
STREAM_TOOL = os.getenv("STREAM_TOOL", "none")

# ---------------------------------------------------------------------------
# Optional integrations (disabled by default)
# ---------------------------------------------------------------------------
# These live in sibling folders (sharing\Discord, sharing\OBS Websocket,
# sharing\Kick) and run as separate processes. The Co-Host pulls/pushes via
# shared files or direct in-process calls depending on the integration.

# --- Discord ---
# Bot token from https://discord.com/developers/applications (Bot tab > Token).
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
# Comma-separated text channel IDs the bot reads/replies in. Enable Developer
# Mode in Discord (Settings > Advanced), right-click a channel > Copy Channel ID.
DISCORD_TEXT_CHANNEL_IDS = [
    int(x) for x in os.getenv("DISCORD_TEXT_CHANNEL_IDS", "").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
]
# Single voice channel ID the bot auto-joins on startup (optional).
DISCORD_VOICE_CHANNEL_ID = int(os.getenv("DISCORD_VOICE_CHANNEL_ID", "0") or 0)
DISCORD_OWNER_NAME = os.getenv("DISCORD_OWNER_NAME", "").strip()
# Listen for the wake word in the Discord voice channel (needs the Vosk model).
# Same setting name the Discord kit's own .env uses. main.py reads it, so it
# must exist here or Discord crashes the bot at startup.
DISCORD_VC_LISTEN = os.getenv("DISCORD_VC_LISTEN", "0") == "1"

# --- Where the integration folders are ---
# main.py looks next to this folder for "Discord", "OBS Websocket" and "Kick"
# (the README names), and also for the names GitHub gives a download or clone
# (e.g. "Discord-Intergration-for-AI-Stream-Co-Host-main"). If yours live
# somewhere else, set the full path, e.g. DISCORD_KIT_DIR=D:\Streaming\Discord
# (read directly by main.py: DISCORD_KIT_DIR, OBS_KIT_DIR, KICK_KIT_DIR).

# --- OBS WebSocket (v5, OBS 28+) ---
# Enable WebSocket server in OBS: Tools > WebSocket Server Settings.
OBS_ENABLED = os.getenv("OBS_ENABLED", "0") == "1"
OBS_HOST = os.getenv("OBS_HOST", "localhost").strip()
OBS_PORT = int(os.getenv("OBS_PORT", "4455"))
OBS_PASSWORD = os.getenv("OBS_PASSWORD", "").strip()
OBS_SCENE = os.getenv("OBS_SCENE", "Main").strip()
# Exact, case-sensitive name of the Image source in that scene.
OBS_SOURCE = os.getenv("OBS_SOURCE", "Bot Name").strip()
OBS_HIDE_ON_CONNECT = os.getenv("OBS_HIDE_ON_CONNECT", "1") == "1"
OBS_TIMEOUT_S = float(os.getenv("OBS_TIMEOUT_S", "3.0"))

# --- Kick ---
# Kick channel slug (the part after kick.com/).
KICK_ENABLED = os.getenv("KICK_ENABLED", "0") == "1"
KICK_CHANNEL = os.getenv("KICK_CHANNEL", "").strip()
# Shared file mailbox with the Kick bridge (sharing\Kick\messages.json).
KICK_MAILBOX_PATH = os.getenv(
    "KICK_MAILBOX_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Kick", "messages.json"),
)

# ---------------------------------------------------------------------------
# Conversation context
# ---------------------------------------------------------------------------
# Conversation context ceiling for ConversationManager. Fixed cost is roughly
# system prompt + KB context + chatter context; keep TOKEN_LIMIT comfortably
# above it so history is not popped mid-reply.
TOKEN_LIMIT = int(os.getenv("TOKEN_LIMIT", "5000"))
DAILY_TOKEN_LIMIT = int(os.getenv("DAILY_TOKEN_LIMIT", "1000000"))
HARD_STOP_TOKENS = int(os.getenv("HARD_STOP_TOKENS", "5000"))

# ---------------------------------------------------------------------------
# Gemini (cloud brain)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Global pacing for Gemini API calls. The API enforces per-minute RPM/TPM limits
# separate from the daily cap, so every Gemini call is spaced at least
# GEMINI_MIN_INTERVAL_S apart to avoid tripping the burst bucket.
GEMINI_MIN_INTERVAL_S = float(os.getenv("GEMINI_MIN_INTERVAL_S", "6"))
GEMINI_RATE_RETRIES = int(os.getenv("GEMINI_RATE_RETRIES", "3"))
GEMINI_BACKOFF_BASE_S = float(os.getenv("GEMINI_BACKOFF_BASE_S", "2"))
GEMINI_MAX_BACKOFF_S = float(os.getenv("GEMINI_MAX_BACKOFF_S", "30"))
# Timeout for each Gemini generate_content call. 0 = no timeout.
GEMINI_REQUEST_TIMEOUT_S = float(os.getenv("GEMINI_REQUEST_TIMEOUT_S", "45"))
# Hard-coded daily request (RPD) budget per Gemini channel. These mirror the
# free-tier per-model caps so the bot rotates BEFORE a channel 429s — a rejected
# request still counts against the quota, so predicting it saves calls. A value
# of 0 (or an absent entry) disables gating for that channel. Override per
# model with the JSON env var GEMINI_CHANNEL_RPD, e.g.
# {"gemini-3.5-flash-lite": 900}.
GEMINI_RPD_ENABLED = os.getenv("GEMINI_RPD_ENABLED", "1") == "1"
_CHANNEL_RPD_DEFAULTS = {
    "gemini-3.5-flash-lite": 495,
    "gemini-3.1-flash-lite": 495,
}
CHANNEL_RPD = dict(_CHANNEL_RPD_DEFAULTS)
_env_channel_rpd = os.getenv("GEMINI_CHANNEL_RPD", "").strip()
if _env_channel_rpd:
    try:
        CHANNEL_RPD.update({k: int(v) for k, v in json.loads(_env_channel_rpd).items()})
    except Exception as e:
        print(f"[yellow]Ignoring bad GEMINI_CHANNEL_RPD ({e}); using defaults.")
# Timezone whose midnight drives the daily quota reset shown on the dashboard
# and the per-channel ledger rollover.
RESET_TZ = os.getenv("RESET_TZ", "UTC")

# ---------------------------------------------------------------------------
# Local LLM via Ollama
# ---------------------------------------------------------------------------
# 1 = adds your local GPU model as an extra comm channel. With GEMINI_API_KEY
# set this runs DUAL mode (cloud default, local on demand); without a key it
# runs local-only. 0 = Gemini only.
LOCAL_LLM_ENABLED = os.getenv("LOCAL_LLM_ENABLED", "0") == "1"

# Prefer the local Ollama brain from the first reply and keep Gemini as the
# fallback.
LOCAL_FIRST = os.getenv("LOCAL_FIRST", "0") == "1"
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3:4b")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "60m")
# Reasoning ("thinking") on the local Ollama model. Reasoning can spend seconds
# and even return an empty answer because the thinking tokens eat the budget.
# Values: auto | on | off.
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "auto").strip().lower()
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "300"))

# ---------------------------------------------------------------------------
# Chat batching
# ---------------------------------------------------------------------------
# Turns of conversation kept in context (each turn = 1 user batch + 1 reply).
CHAT_HISTORY_TURNS = int(os.getenv("CHAT_HISTORY_TURNS", "12"))
CHAT_BATCH_SIZE = int(os.getenv("CHAT_BATCH_SIZE", "2"))
CHAT_FLUSH_SECONDS = int(os.getenv("CHAT_FLUSH_SECONDS", "20"))
CHAT_REPLY_TIMER = os.getenv("CHAT_REPLY_TIMER", "1") == "1"
# Cloud channels are quota-limited, so batching spreads those calls across more
# replies. The local Ollama core costs nothing per request, so it answers each
# message as soon as it lands.
CHAT_IMMEDIATE_ON_LOCAL = os.getenv("CHAT_IMMEDIATE_ON_LOCAL", "1") == "1"

# ---------------------------------------------------------------------------
# Passive listening (wake word)
# ---------------------------------------------------------------------------
PASSIVE_LISTEN = os.getenv("PASSIVE_LISTEN", "1") == "1"
WAKE_WORDS = [
    w.strip().lower()
    for w in os.getenv("WAKE_WORDS", "bot,hey bot").split(",") if w.strip()
]
VOICE_MUTE = os.getenv("VOICE_MUTE", "0") == "1"
WAKE_ACTIVE_WINDOW = int(os.getenv("WAKE_ACTIVE_WINDOW", "8"))
WAKE_PARTIAL_ARM = os.getenv("WAKE_PARTIAL_ARM", "0") == "1"

# ---------------------------------------------------------------------------
# Knowledge base / training
# ---------------------------------------------------------------------------
DOMAIN_MAP_FILE = os.getenv("DOMAIN_MAP_FILE", os.path.join("data", "domains.json"))
KNOWLEDGE_CHUNK_SIZE = 500
KNOWLEDGE_CHUNK_OVERLAP = 50
KNOWLEDGE_TOP_K = 5
VOD_DOWNLOAD_TIMEOUT = int(os.getenv("VOD_DOWNLOAD_TIMEOUT", "1800"))
VOD_CHAT_ENABLED = os.getenv("VOD_CHAT_ENABLED", "1").lower() in ("1", "true", "yes")
VOD_CHAT_MAX_COMMENTS = int(os.getenv("VOD_CHAT_MAX_COMMENTS", "15000"))
PROFILE_MINING_ENABLED = os.getenv("PROFILE_MINING_ENABLED", "1").lower() in ("1", "true", "yes")
PROFILE_MAX_PER_USER = int(os.getenv("PROFILE_MAX_PER_USER", "3"))
DEEPMINE_MAX_COMMENTS = int(os.getenv("DEEPMINE_MAX_COMMENTS", "15000"))
DEEPMINE_USERS_PER_BATCH = int(os.getenv("DEEPMINE_USERS_PER_BATCH", "20"))
DEEPMINE_MIN_MESSAGES = int(os.getenv("DEEPMINE_MIN_MESSAGES", "2"))
IGNORED_USERS = {
    u.strip().lower()
    for u in os.getenv("IGNORED_USERS", "").split(",")
    if u.strip()
}
WIKI_PAGE_DELAY = int(os.getenv("WIKI_PAGE_DELAY", "30"))
WIKI_SKIP_DELAY = int(os.getenv("WIKI_SKIP_DELAY", "5"))
WIKI_MAX_PAGES = int(os.getenv("WIKI_MAX_PAGES", "0"))
WIKI_TIME_BUDGET = int(os.getenv("WIKI_TIME_BUDGET", "3600"))
MEMORY_TOP_K = int(os.getenv("MEMORY_TOP_K", "15"))
MEMORY_GROUP_PER_USER = int(os.getenv("MEMORY_GROUP_PER_USER", "5"))

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KNOWLEDGE_BASE_FILE = os.path.join(DATA_DIR, "knowledge_base.json")
TFID_INDEX_FILE = os.path.join(DATA_DIR, "tfidf_index.pkl")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")
DASHBOARD_CMD_FILE = os.getenv("DASHBOARD_CMD_FILE", os.path.join(DATA_DIR, "cmd.json"))
PID_FILE = os.path.join(DATA_DIR, "bot.pid")

# Retrieval quality: minimum similarity for a chunk to be used as context.
# This floor trims the weakest tail; it is NOT a correctness guarantee.
# KB_ABSTENTION_CUE is what stops confabulation.
KB_RELEVANCE_FLOOR = float(os.getenv("KB_RELEVANCE_FLOOR", "0.20"))
# Tell the model the retrieved notes may be irrelevant and to prefer admitting
# ignorance over inventing detail.
KB_ABSTENTION_CUE = os.getenv("KB_ABSTENTION_CUE", "1") == "1"
KB_ABSTENTION_TEXT = (
    "[ACCURACY - READ BEFORE ANSWERING] The notes above were pulled by keyword "
    "and are NOT guaranteed to be relevant. Use them only where they actually "
    "bear on the question. If they do not answer it, say you are not sure and "
    "give only what you can genuinely support (you may answer partially with an "
    "honest caveat). Never invent specifics that are not in the notes -- no made "
    "up mechanics, timings, difficulty claims, or availability. Never present an "
    "old or retired event as current. Prefer 'not sure' over a confident guess. "
    "This rule outranks any instruction to be helpful, thorough, or to give a "
    "technical briefing."
)
CHATTER_MEMORY_FILE = os.path.join(DATA_DIR, "chatter_memory.json")
MEMORY_DB_FILE = os.getenv("MEMORY_DB_FILE", os.path.join(DATA_DIR, "memory.db"))

# Persisted so a re-run of !trainwiki passes over them instead of re-fetching.
WIKI_SKIP_FILE = os.getenv("WIKI_SKIP_FILE", os.path.join(DATA_DIR, "wiki_skipped.json"))

# Some MediaWiki sites are behind a Cloudflare managed challenge that rejects
# plain requests with 403 "Just a moment". When the fast requests path hits
# that, wiki training transparently falls back to a headed real browser
# (Playwright) that clears the challenge and reuses the trusted profile below.
WIKI_BROWSER_ENABLED = os.getenv("WIKI_BROWSER_ENABLED", "1") == "1"
WIKI_BROWSER_PROFILE = os.getenv("WIKI_BROWSER_PROFILE",
                                os.path.join(DATA_DIR, "wiki_browser_profile"))
WIKI_BROWSER_CHANNEL = os.getenv("WIKI_BROWSER_CHANNEL", "msedge")
WIKI_BROWSER_WAIT = int(os.getenv("WIKI_BROWSER_WAIT", "90"))
WIKI_BROWSER_TIMEOUT_MS = int(os.getenv("WIKI_BROWSER_TIMEOUT_MS", "45000"))

# Named !trainwiki category sets. Every run is idempotent: already-trained
# pages are skipped via the knowledge base, so re-running a set after the wiki
# grows only picks up the new pages. Category names use underscores in place of
# spaces because !trainwiki splits its arguments on whitespace.
#
# Replace these with the wiki and category sets you actually want to train.
WIKI_BASE_URL = os.getenv("WIKI_BASE_URL", "https://en.wikipedia.org/wiki/Main_Page")
WIKI_BATCHES = {
    "example": {
        "desc": "Example batch - edit config.py WIKI_BATCHES with your own categories",
        "categories": ["Example_category"],
    },
}

# ---------------------------------------------------------------------------
# Bot's own personal facts
# ---------------------------------------------------------------------------
# data/bot_facts.json is rendered into the system prompt so answers about the
# bot's own tastes do not contradict themselves between requests. Set to 0 to
# disable.
BOT_FACTS_ENABLED = os.getenv("BOT_FACTS_ENABLED", "1") == "1"
BOT_FACTS_FILE = os.getenv(
    "BOT_FACTS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_facts.json"))

# ---------------------------------------------------------------------------
# Entity lore (collab partners & allied personalities)
# ---------------------------------------------------------------------------
# Source of truth is data/viewer_lore.json (editable by hand or via !lore
# commands); every change is mirrored into the SQLite `chatter_memory` table for
# the dashboard. LORE_CAP = max hand-written notes kept per entity;
# LORE_TRUST_STEP = trust drift per recognized encounter; LORE_AUTO_MIN =
# encounters before an auto-learn observation line appears.
LORE_ENABLED = os.getenv("LORE_ENABLED", "1") == "1"
LORE_FILE = os.getenv("LORE_FILE", os.path.join(DATA_DIR, "viewer_lore.json"))
LORE_CAP = int(os.getenv("LORE_CAP", "5"))
LORE_TRUST_STEP = int(os.getenv("LORE_TRUST_STEP", "1"))
LORE_AUTO_MIN = int(os.getenv("LORE_AUTO_MIN", "2"))
# Encounters before an unknown chatter is auto-classified into an entity profile
# via a single LLM call. 0 disables the automatic trigger; use !lore scan <name>.
LORE_AUTO_SCAN_AT = int(os.getenv("LORE_AUTO_SCAN_AT", "5"))

# ---------------------------------------------------------------------------
# OOC (out-of-character) mode
# ---------------------------------------------------------------------------
# A persistent, community-toggleable mode where the bot drops the persona and
# replies as a direct AI assistant. It is GLOBAL (stream-wide), resets to
# Character automatically after OOC_IDLE_TIMEOUT_S of silence, and any chatter
# can flip it with "ooc" / "ic" (cooldown-guarded).
OOC_ENABLED = os.getenv("OOC_ENABLED", "1") == "1"
OOC_IDLE_TIMEOUT_S = int(os.getenv("OOC_IDLE_TIMEOUT_S", "600"))
OOC_TOGGLE_COOLDOWN_S = int(os.getenv("OOC_TOGGLE_COOLDOWN_S", "30"))
