# Commands

Commands work in Twitch chat **or** typed directly into the console window.
Owner-only commands are gated to the logins listed in `STREAMER_ALIASES`.

## Chat

| Command | What it does |
| --- | --- |
| `!ask <message>` | Ask the co-host a question. |
| `!remember <fact>` | Save a fact about yourself to long-term memory (SQLite). |
| `!forget <fact>` | Remove one of your saved facts. |
| `!memory` | Show what the bot remembers about you. |
| `!help` | List commands in the console. |

Mentioning `TWITCH_MENTION_NAME` anywhere in a chat message also gets the
bot's attention, with or without `!ask`.

## Conversation

| Command | Access | What it does |
| --- | --- | --- |
| `!clear` | everyone | Clear the rolling conversation history. |
| `!ooc` / `!ic` | everyone | Flip the whole stream out of / back into character. Any chatter can also just say `ooc` or `ic`. |
| `!voice on\|off\|status` | owner | Mute or unmute the voice. Silent mode still replies in chat; it just does not speak. |
| `!ears on\|off\|restart\|status` | owner | Control the passive microphone listener / wake word. |
| `!mute twitch-chat on\|off` | owner | Drop incoming Twitch chat without replying. |
| `!mute twitch-live on\|off` | owner | Stop checking whether the channel is live. |
| `!mute all on\|off` | owner | Apply the mute switches together. |
| `!mute status` | owner | Show the current mute state. |

## Brain and channels

| Command | Access | What it does |
| --- | --- | --- |
| `!freq <n>` / `!frequency <n>` | owner | Switch to comm channel `n` (a Gemini model or the local Ollama model, depending on config). |

With `GEMINI_API_KEY` set and `LOCAL_LLM_ENABLED=1`, the bot runs dual mode:
Gemini is the default and the local Ollama model is available as its own
channel. Without a Gemini key it runs local-only.

## Training

| Command | Access | What it does |
| --- | --- | --- |
| `!train <url>` | owner | Train on a YouTube video, Twitch VOD, website, or local file. Twitch VODs also ingest the chat replay (`VOD_CHAT_ENABLED`) and mine it into per-viewer profiles (`PROFILE_MINING_ENABLED`). |
| `!traindir <path>` | owner | Train on every video in a directory. |
| `!trained` | everyone | List everything in the knowledge base. |
| `!forget_video <name>` | owner | Remove one trained entry. |
| `!rebuild_index` | owner | Rebuild the TF-IDF search index. |
| `!trainwiki <url> [max]` | owner | Train every page of a MediaWiki site through its API. Optional category scoping: `!trainwiki <url> category:Physics,Chemistry`. Underscores stand for spaces. Re-running resumes: already-trained pages are skipped. |
| `!wikirefresh <url> [force]` | owner | Re-fetch already-trained pages from a wiki and update only the ones that changed. |
| `!batch [name]` | owner | Run or list the named wiki category sets defined in `config.py` (`WIKI_BATCHES`). |
| `!deepmine <url>` | owner | Mine a Twitch VOD's chat for viewer facts and references to the streamer. Runs in batches and spends daily token budget. |

Training is polite by default: `WIKI_PAGE_DELAY=30` seconds between pages and
a `WIKI_TIME_BUDGET=3600` second session cap. Both are configurable in `.env`.

## Knowledge domains

A domain groups a set of hosts and keywords (see `data/domains.json`). New
sources are sorted into a domain by host at ingest time.

| Command | Access | What it does |
| --- | --- | --- |
| `!topics` | everyone | List domains with entry/chunk counts and the active lens. |
| `!topic <name>` | owner | Pin retrieval to one domain. |
| `!topic all` | owner | Clear the pin. |
| `!forget_domain <name>` | owner | Remove every trained entry in a domain. |

Without a pin, the bot auto-focuses using keyword matches in the question and
searches everything when nothing matches.

## Entity lore

Entity lore tracks collab partners and recurring chatters, with a trust score
and hand-written notes. It lives in `data/viewer_lore.json` and can also be
edited from chat.

| Command | Access | What it does |
| --- | --- | --- |
| `!lore` | owner | List known entities. |
| `!lore <name>` | owner | Show one entity's profile. |
| `!lore add <name> <note>` | owner | Add a note. |
| `!lore remove <name> <index>` | owner | Remove a note. |
| `!lore scan <name>` | owner | Classify a chatter from their recent messages. |
| `!lore alias <name> <alias>` | owner | Attach another account name to the same entity. |

Roles and per-entity notes are optional; a fresh install starts with none.

## Not included in this build

Discord, Kick, and OBS WebSocket integrations are intentionally absent. The
dashboard shows inert **INTEGRATE** pills in their place so the layout documents
what could be added later.
