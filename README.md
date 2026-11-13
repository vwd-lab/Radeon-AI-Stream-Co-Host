# This is a Beta
This is a Beta of Radeon AI Stream Co-Host. Feedback would be appricated to help improve it and make it better.

# Radeon AI Stream Co-Host

A Twitch AI co-host that reads chat, answers with a cloud or local language
model, and speaks the replies out loud with a cloned voice. It ships with an
LCARS-style status dashboard, a wake-word microphone mode, a long-term memory
of viewers, and a trainable knowledge base.

Everything runs on one Windows machine. Discord, Kick, and OBS are optional
integrations that live in sibling folders and are disabled by default.

## What it does

- **Twitch chat** — reads chat, answers `!ask`, and reacts when its name is
  mentioned. Replies can be spoken, posted as text, or both.
- **Two brains, one personality** — Google Gemini by default; a local Ollama
  model can run alongside it as a separate channel, or on its own with no API
  key. `!freq` switches channels.
- **Cloned voice** — Pocket TTS runs as a local HTTP server and speaks in a
  voice cloned from one reference WAV. CPU only, so the GPU stays free.
- **Memory** — SQLite-backed long-term facts per viewer (`!remember`,
  `!memory`), plus rolling conversation history with a token budget.
- **Knowledge base** — train on YouTube videos, Twitch VODs, websites, local
  media, and whole MediaWiki sites, then retrieve the relevant chunks per
  question. Domains let you scope retrieval.
- **Microphone** — wake-word passive listening or push-to-talk on `F4`, using
  Vosk for offline speech recognition.
- **OOC mode** — any chatter can flip the stream out of character (`ooc` /
  `ic`); it resets automatically after a quiet period.
- **Dashboard** — a separate LCARS-style window with model status, latency,
  token budget, mute/voice/ears controls, and an activity log.

## Requirements

- Windows 10/11
- Python 3.12 (the launchers call `python` from `PATH`)
- A microphone and speakers/headphones for voice features
- Optional: a Twitch bot account and chat token
- Optional: a Gemini API key, and/or [Ollama](https://ollama.com/) for a local
  model
- Optional: a Vosk model for microphone input

## Quick start

### 1. Install the Python dependencies

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` covers chat, the dashboard, memory, and training. Whisper
is only needed to transcribe audio that has no captions, and pulls in torch:

```bat
pip install -r requirements-training.txt
```

### 2. Configure

#### Quick way — setup GUI

Double-click `setup_gui.bat` (or run `python setup_gui.py`). It opens a tkinter
window where you set **Bot name** (`BOT_NAME`), **Streamer name** (`STREAMER_NAME`)
and related Twitch identity fields, then writes them to `.env` for you — no need
to edit the file by hand. Restart `start_bot.bat` for the change to take effect.

#### Manual way — edit `.env` directly

```bat
copy .env.example .env
```

At minimum set:

```ini
BOT_NAME="Bot Name"
STREAMER_NAME="Streamer"
```

Then, to connect Twitch:

```ini
TWITCH_OAUTH_TOKEN=oauth:...
TWITCH_BOT_NICK=yourbotlogin
TWITCH_CHANNEL=yourchannel
```

Generate the token at <https://twitchapps.com/tmi/> while logged in as the bot
account. `TWITCH_CHANNEL` is the channel to join, without the `#`.

For the brain, pick one:

```ini
# Cloud
GEMINI_API_KEY=your-key

# Local only (no cloud key needed)
LOCAL_LLM_ENABLED=1
OLLAMA_MODEL=gemma3:4b
```

Pull the model first: `ollama pull gemma3:4b`. Any model you have installed
works; the tag just has to match `OLLAMA_MODEL`.

**Owner commands** (`!train`, `!voice`, `!mute`, `!freq`, dashboard buttons…)
work automatically for you from the console, the microphone, the dashboard,
and your Twitch account. Your Twitch account is recognised because it owns
`TWITCH_CHANNEL`. You don't need to list yourself anywhere.

`STREAMER_ALIASES` is optional, for *extra* accounts that should also get owner
commands. Kick names are checked as `kick:<name>`, so a Kick account that
happens to share your Twitch login does **not** get owner commands; add your
own Kick account with the prefix:

```ini
STREAMER_ALIASES=youralttwitchlogin,kick:yourkickname
```

Don't put `STREAMER_NAME` or `streamer` in it unless that is a login you own:
anyone with that login would get owner commands. The bot warns at startup if
it sees one.

### 3. Add a Vosk model (microphone only)

Download a small English model from <https://alphacephei.com/vosk/models> and
unzip it so this path exists:

```
models\vosk-model-small-en-us\
```

Set `PASSIVE_LISTEN=0` if you only want push-to-talk, or skip this step
entirely if you do not need the microphone.

### 4. Set up Pocket TTS (voice)

Pocket runs in its own CPU venv so it does not fight the GPU for memory:

```bat
python -m venv pocket-tts-venv
pocket-tts-venv\Scripts\pip install pocket-tts
```

Put a clean 10–20 second mono WAV of the voice you want at
`data\speaker_reference.wav`, then export it once:

```bat
pocket-tts-venv\Scripts\python.exe tools\export_pocket_voice.py
```

That writes `data\pocket_voice.safetensors`. The launcher starts the server
automatically on port 8123. Set `POCKET_VENV` in the environment if your venv
lives somewhere else.

### 5. Run

```bat
start_bot.bat
```

The launcher brings up Pocket TTS first, then the bot. To stop both:

```bat
start_bot.bat stop
```

In a second window, start the dashboard:

```bat
dashboard.bat
```

The dashboard reads `data\status.json` and sends button commands through
`data\cmd.json`. It can run before or after the bot.

## Project layout

```
main.py                  Main loop, command dispatch, chat batching
config.py                Every setting and default, loaded from .env
setup_gui.py             tkinter GUI to set BOT_NAME / STREAMER_NAME → .env
setup_gui.bat            Double-click launcher for setup_gui.py
dashboard.py             LCARS status dashboard (PySide6)
core/
  conversation.py        Persona prompts and token-aware history
  ai_engine.py           Gemini + Ollama engines, failover, VRAM release
  intel_pipeline.py      Prompt assembly and provider routing
  knowledge_base.py      TF-IDF retrieval over trained chunks
  viewer_lore.py         Entity lore and trust scoring
  chatter_memory.py      Per-viewer profiles and recent messages
  bot_facts.py           The bot's own preferences, from data/bot_facts.json
  status_feed.py         Writes the dashboard snapshot
input/
  twitch_chat.py         Twitch IRC client
  mic_input.py           Vosk wake-word and push-to-talk
tts/
  base_tts.py            Text cleanup, chunking, lazy engine wrapper
  pocket_tts.py          HTTP client for the Pocket server
training/                Website, wiki, YouTube, Twitch VOD ingestion
memory.py                SQLite for facts, streaks, raids
streaming/               Optional stream-tool hook (Streamlabs stub)
tools/                   Voice export helper
data/                    Runtime state, facts, lore, knowledge base
```

## Configuration notes

- **Chat batching.** Cloud models have quotas, so chat messages are batched
  (`CHAT_BATCH_SIZE`, default 2) or flushed after `CHAT_FLUSH_SECONDS` (20).
  When the local model is active, `CHAT_IMMEDIATE_ON_LOCAL=1` answers at once.
- **Token budget.** `DAILY_TOKEN_LIMIT` is the daily spend across channels.
  The bot shuts down when it is exhausted. `TOKEN_LIMIT` is the context window;
  the two are unrelated.
- **Knowledge base.** `data\knowledge_base.json` holds trained chunks and
  `data\tfidf_index.pkl` the search index. Both are generated. Delete them to
  start over.
- **Domains.** `data\domains.json` maps hosts and keywords to domain names.
  `!topic <name>` narrows retrieval; `!topic all` clears it.
- **Personality.** Edit `core\conversation.py` for the voice, and
  `data\bot_facts.json` for the bot's own opinions and history. Restart after
  editing either.
- **Lore.** `data\viewer_lore.json` starts empty. Add collab partners by hand
  or with `!lore`.

See [COMMANDS.md](COMMANDS.md) for every chat command.

## Optional integrations

Discord, Kick, and OBS WebSocket integrations live in **sibling folders** under
`sharing/` and run as separate processes. The Co-Host pulls/pushes from them
via shared files or direct in-process calls. All are **disabled by default**
— set the matching `*=1` flag in `.env` and restart `start_bot.bat` to enable.

| Integration | Sibling folder | How it connects |
|---|---|---|
| **Discord** | `sharing\Discord` | `DiscordBridge` gateway → `on_text` callback → Co-Host brain; replies posted back via `send_text()` |
| **Kick** | `sharing\Kick` | Shared `messages.json` mailbox (`KICK_MAILBOX_PATH`); bridge posts replies to Kick chat |
| **OBS WebSocket** | `sharing\OBS Websocket` | `ObsStaticOverlay` toggles a single image source on/off while the bot speaks |

Each sibling folder has its own `setup_*_gui.bat` (interactive setup) and
`start_*_gui.bat` (launcher). See that folder's `README.md` for the full setup
sequence (bot token, tunnel URL, OBS WebSocket credentials, etc.).

**Folder names.** The bot looks next to its own folder for `Discord`,
`OBS Websocket` and `Kick`, and also for the names GitHub gives them when you
download or clone (for example `Discord-Intergration-for-AI-Stream-Co-Host-main`),
so you don't need to rename anything. If a folder lives somewhere else, put its
full path in `.env`:

```ini
DISCORD_KIT_DIR=D:\Streaming\Discord
OBS_KIT_DIR=D:\Streaming\OBS Websocket
KICK_KIT_DIR=D:\Streaming\Kick
```

If an integration can't be loaded (folder missing, a package not installed),
the bot prints a yellow "unavailable, continuing without it" line with the
reason and carries on without it.

The LCARS dashboard (`dashboard.bat`) shows live **ONLINE / OFFLINE** pills for
Kick, Discord, and OBS, reflecting status from the status feed.

## Not included

There is no PNGTuber avatar director and no OBS scene control beyond the
single static-image overlay. The full multi-state avatar (talk frames, blink,
smug, thinking/listening) from the original Logan build is not part of this
distribution.


## Troubleshooting

**The bot starts but never speaks.**
Check the minimised `pocket-tts` window. `start_pocket_tts.bat` prints why it
skipped: missing venv, missing voice state, or a failed health check. Voice is
optional at startup; the bot keeps running without it.

**Pocket says the voice state is missing.**
`data\speaker_reference.wav` must exist before you run
`tools\export_pocket_voice.py`. The export writes
`data\pocket_voice.safetensors`.

**Twitch does not connect.**
`TWITCH_OAUTH_TOKEN`, `TWITCH_BOT_NICK`, and `TWITCH_CHANNEL` must all be set.
The token starts with `oauth:` and belongs to the bot account. Check the
console for the exact rejection message.

**Commands do nothing for the streamer.**
From Twitch, check that `TWITCH_CHANNEL` is your own channel (without the `#`):
its owner is the streamer. From the console, microphone and dashboard, owner
commands always work. From Kick, add `kick:yourkickname` to `STREAMER_ALIASES`.

**The bot exits immediately saying no brain is configured.**
Set `GEMINI_API_KEY`, or set `LOCAL_LLM_ENABLED=1` and make sure Ollama is
running with the model named in `OLLAMA_MODEL`.

## Security

`.env` is git-ignored. Never commit real tokens, API keys, database files, or
voice samples. The repository ships `.env.example` only.

## License

This project uses a custom **source-available** license, not an open-source
license. You may use, copy, and modify it for personal, non-commercial
purposes, including **using it during a livestream** (Twitch, YouTube, Kick,
etc.) as long as the stream is not a paid service tied to the Software. You may
**not** redistribute it, publish modified versions, or make forks available to
third parties. See [LICENSE](LICENSE) for the full terms.
