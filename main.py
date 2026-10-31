# =============================================================================
# Radeon AI Co-Host
#
# A Twitch AI co-host: reads Twitch chat, answers through an LLM (Gemini,
# local Ollama, or both), and speaks replies out loud with Pocket TTS.
# =============================================================================

import os
import re
import sys
import time
import json
import queue
import uuid
import threading
import atexit
import importlib.util
import keyboard
from rich import print

import config
from core.conversation import ConversationManager, BOT_SYSTEM_PROMPT, OOC_SYSTEM_PROMPT
from core.ai_engine import AiEngine, LocalAiEngine, DualEngine
from core.chatter_memory import ChatterMemory
from core.knowledge_base import KnowledgeBase
from training.video_processor import VideoProcessor
from tts.base_tts import LazyTTS, split_text_for_tts
from tts.pocket_tts import PocketTTS
from core.status_feed import STATUS_FEED
from audio.player import AudioManager
from memory import MemoryDB
from input.mic_input import MicInput, PassiveListener
from input.twitch_chat import TwitchChatListener
from streaming.streamlabs_stream import StreamlabsStreamTool
from core.intel_pipeline import generate_bot_response
from core.rate_limiter import backoff_delay, channel_rpd_exhausted, classify_gemini_error, is_transport_error
from core.viewer_lore import ViewerLoreManager
from core.ooc_mode import OocMode
from core.voice_mute import is_voice_muted, set_voice_muted
from core.chat_mute import is_chat_muted, set_chat_muted
from core.twitch_live import set_enabled as twitch_live_set_enabled
from core.twitch_live import is_enabled as twitch_live_tracking_enabled

# ---------------------------------------------------------------------------
# Optional integrations (sibling folders, run as separate processes)
# ---------------------------------------------------------------------------
DISCORD_KIT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "Discord"))
OBS_KIT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "OBS Websocket"))
KICK_KIT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "Kick"))

for _kit in (DISCORD_KIT, OBS_KIT, KICK_KIT):
    if _kit not in sys.path:
        sys.path.insert(0, _kit)


def init_stream_tool():
    if config.STREAM_TOOL == "streamlabs":
        return StreamlabsStreamTool()
    return None


def _load_sibling_module(module_name, file_path):
    """Load a module from a sibling kit folder by file path.

    This avoids clobbering Co-Host's own ``input`` package (both the Discord
    and OBS kits ship their own ``input/`` package). Each module is registered
    under a unique name so it never collides with Co-Host's modules.
    """
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_streamer(username):
    """True when this chatter is the streamer (streamer-only commands).

    Matches any spelling in config.STREAMER_ALIASES rather than comparing
    against config.STREAMER_NAME directly. Chat usernames arrive as bare
    logins like "yourchannelname", so an equality test against a spaced display
    name silently fails and quietly disables every owner-gated command.
    """
    if not username:
        return False
    return username.strip().lower() in config.STREAMER_ALIASES


# Owner-only commands that mutate the knowledge base or global state. These
# sat above the existing _is_streamer() block, so any chatter in chat could
# reach them - including !forget_video, which deletes a trained video, and
# !traindir, which can train a whole directory. Gated here, before dispatch.
_ADMIN_ONLY_COMMANDS = ("!train", "!traindir", "!forget_video",
                        "!rebuild_index", "!clear")


def _is_admin_command(text_lower):
    head = text_lower.split(" ", 1)[0]
    return head in _ADMIN_ONLY_COMMANDS


def handle_special_command(text, username, chatter_memory, video_processor, knowledge_base, conversation, memory_db=None, ai_engine=None):
    text_lower = text.strip().lower()

    if _is_admin_command(text_lower) and not _is_streamer(username):
        # Swallowed rather than passed to the model: it is a command attempt,
        # not a question, and a refusal line would just be read aloud.
        print(f"[red]Blocked owner-only '{text_lower.split()[0]}' from "
              f"{username or '(unknown)'}")
        return True

    if text_lower.startswith("!remember "):
        fact = text[len("!remember "):].strip()
        if memory_db:
            memory_db.add_memory(username, fact)
        return True

    if text_lower.startswith("!forget "):
        fact = text[len("!forget "):].strip()
        if memory_db:
            memory_db.forget(username, fact)
        return True

    if text_lower == "!memory":
        if memory_db:
            memories = memory_db.list_for(username)
            count = len(memories)
            if count:
                print(f"[cyan]Long-term memories for {username} ({count}):")
                for i, (fact, created) in enumerate(memories, 1):
                    print(f"[cyan]  {i}. {fact}   [saved {created[:10]}]")
            else:
                print(f"[cyan]No long-term memories yet for {username}")
        if chatter_memory:
            profile = chatter_memory.get_profile(username)
            if profile["interaction_count"]:
                print(f"[cyan]{username}: {profile['interaction_count']} total interactions")
        return True

    if text_lower.startswith("!train "):
        url = text[len("!train "):].strip()
        print(f"[yellow]Training on: {url}")
        video_processor.train_url(url)
        return True

    if text_lower.startswith("!traindir "):
        dir_path = text[len("!traindir "):].strip()
        video_processor.train_directory(dir_path)
        return True

    if text_lower.startswith("!forget_video "):
        title = text[len("!forget_video "):].strip()
        video_processor.forget_video(title)
        return True

    if text_lower == "!trained":
        videos = knowledge_base.list_videos()
        if videos:
            for vid_id, vid_data in videos.items():
                print(f"[cyan]  - {vid_data['title']} ({vid_data['source']}, {vid_data['chunk_count']} chunks)")
        else:
            print("[cyan]No videos trained yet")
        return True

    if text_lower == "!rebuild_index":
        knowledge_base.rebuild_index()
        return True

    if text_lower == "!clear":
        conversation.clear_history()
        print("[cyan]Conversation history cleared")
        return True

    if text_lower == "!help":
        print("[cyan]Available commands:")
        print(f"[cyan]  !ask <message>       - Ask {config.BOT_NAME} a question")
        print("[cyan]  !remember <fact>     - Save a fact about yourself")
        print("[cyan]  !forget <fact>       - Remove a fact")
        print(f"[cyan]  !memory              - See what {config.BOT_NAME} remembers about you")
        print("[cyan]  !train <url>         - Train on a YouTube/Twitch VOD/website URL")
        print("[cyan]  !traindir <path>     - Train on all videos in a directory")
        print("[cyan]  !trained             - List trained videos")
        print("[cyan]  !forget_video <name> - Remove a trained video")
        print("[cyan]  !rebuild_index       - Rebuild the search index")
        print("[cyan]  !clear               - Clear conversation history")
        print("[cyan]  !freq <n>            - (Streamer) Switch comm channel by number")
        print("[cyan]  !deepmine <url>      - (Streamer) Mine a VOD's chat into viewer profiles")
        print("[cyan]  !trainwiki <url>     - (Streamer) Train every page of a MediaWiki site")
        print("[cyan]  !wikirefresh <url>   - (Streamer) Re-fetch already-trained wiki pages that were edited")
        print("[cyan]  !batch [name]        - (Streamer) Run/list named wiki category sets (idempotent)")
        print("[cyan]  !ears on|off|restart|status - (Streamer) Control passive listening")
        print("[cyan]  !voice on|off|status - (Streamer) Mute/unmute vocals (silent mode = chat replies only)")
        print("[cyan]  !topic <name>|all    - (Streamer) Pin knowledge lens to a domain / reset to ALL")
        print("[cyan]  !topics              - List knowledge domains and counts")
        print("[cyan]  !forget_domain <n>   - (Streamer) Remove an entire knowledge domain")
        print("[cyan]  !help                - Show this help")
        return True

    if _is_streamer(username):
        if text_lower.startswith("!frequency") or text_lower.startswith("!freq"):
            parts = text.split()
            number = parts[1] if len(parts) > 1 else ""
            was_local = getattr(ai_engine, "_using_local", False)
            if ai_engine is None or not ai_engine.set_frequency(number):
                channels = len(ai_engine._models) if ai_engine else 0
                print(f"[red]Invalid frequency '{number}'. Channels: {channels} available.")
                return True
            print(f"[cyan]Channel set to {ai_engine.current_model()}")
            if getattr(ai_engine, "_using_local", False) and not was_local:
                return "Heading to War room... I mean Briefing Room."
            return f"Com Frequency updated (by {username})"

        if text_lower.startswith("!wikirefresh"):
            parts = text.split()
            target = parts[1] if len(parts) > 1 else ""
            if not target:
                print("[red]Usage: !wikirefresh <wiki base url> [force] "
                      "[category:Name1,Name2] [filter] [max_changed]")
                print("[dim]  Re-checks pages already trained from that wiki and re-fetches")
                print("[dim]  only the ones whose content changed. First run records")
                print("[dim]  current revision ids without re-downloading anything.")
                print("[dim]  category: limits the check to pages under those wiki")
                print("[dim]  categories. Pages not yet trained are listed, not updated.")
                print("[dim]    !wikirefresh https://en.wikipedia.org/wiki/Main_Page")
                print("[dim]    !wikirefresh https://en.wikipedia.org/wiki/Main_Page Physics")
                print("[dim]    !wikirefresh https://en.wikipedia.org/wiki/Main_Page category:Physics")
                return True
            force = any(p.lower() == "force" for p in parts[2:])
            limit = None
            contains = None
            categories = None
            for token in parts[2:]:
                if token.lower().startswith("category:"):
                    raw = token[len("category:"):]
                    categories = [c.strip() for c in raw.split(",") if c.strip()]
                elif token.isdigit():
                    limit = int(token)
                elif token.lower() != "force":
                    contains = token
            scope = f" in categories {categories}" if categories else ""
            filt = f" matching '{contains}'" if contains else ""
            mode = " (FORCE: re-fetch every page)" if force else ""
            print(f"[yellow]Refreshing wiki: {target}{scope}{filt}{mode}")
            video_processor.refresh_wiki(target, force=force, contains=contains,
                                        max_changed=limit, categories=categories)
            return True

        if text_lower.startswith("!trainwiki"):
            parts = text.split()
            target = parts[1] if len(parts) > 1 else ""
            if not target:
                print("[red]Usage: !trainwiki <wiki base url> [category:Name1,Name2] [max_pages]")
                return True
            limit = None
            categories = None
            for token in parts[2:]:
                if token.lower().startswith("category:"):
                    raw = token[len("category:"):]
                    categories = [c.strip() for c in raw.split(",") if c.strip()]
                elif token.isdigit():
                    limit = int(token)
            scope = f" in categories {categories}" if categories else ""
            cap = f" (max {limit} pages)" if limit else ""
            print(f"[yellow]Training wiki: {target}{scope}{cap}")
            video_processor.train_wiki(target, max_pages=limit, categories=categories)
            return True

        if text_lower.startswith("!batch"):
            rest = text.split(maxsplit=1)
            args = rest[1].split() if len(rest) > 1 else []
            batches = getattr(config, "WIKI_BATCHES", {}) or {}
            if not args:
                print("[cyan]Named wiki batches (re-runnable; only new pages are fetched):")
                for key, spec in batches.items():
                    print(f"  [cyan]  {key:<12} [dim]{spec.get('desc', '')}")
                print("[cyan]  !batch <name> [max_pages]")
                return True
            name = args[0].lower()
            limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
            if name not in batches:
                print(f"[red]Unknown batch '{name}'. Try !batch for the list.")
                return True
            spec = batches[name]
            categories = spec["categories"]
            print(f"[yellow]Batch '{name}': {spec.get('desc', '')}")
            print(f"[yellow]  {len(categories)} categories, "
                  f"{'no cap' if not limit else f'max {limit} pages'}")
            video_processor.train_wiki(
                config.WIKI_BASE_URL, max_pages=limit, categories=categories)
            return True

        if text_lower.startswith("!deepmine"):
            from training.deep_mine import DeepMiner
            parts = text.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else ""
            if not target:
                print("[red]Usage: !deepmine <twitch VOD url or video id>")
                return True
            if ai_engine is None:
                print("[red]AI engine unavailable")
                return True
            miner = DeepMiner(
                ai_engine=ai_engine, memory_db=memory_db,
                max_comments=config.DEEPMINE_MAX_COMMENTS,
                users_per_batch=config.DEEPMINE_USERS_PER_BATCH,
                min_messages=config.DEEPMINE_MIN_MESSAGES,
            )
            miner.mine_vod(target)
            return True

    return False


def extract_memories(response_text):
    owned = re.findall(r"\[MEM\s*@([^\]:]+):\s*([^\]]+)\]", response_text, flags=re.IGNORECASE)
    plain = re.findall(r"\[MEM:\s*([^\]]+)\]", response_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*\[MEM\s*[@:][^\]]+\]", "", response_text, flags=re.IGNORECASE).strip()
    learned = [(owner.strip(), fact.strip()) for owner, fact in owned if fact.strip()]
    learned.extend([(None, fact.strip()) for fact in plain if fact.strip()])
    return cleaned, learned


def speak_line(text, tts, audio_manager):
    """Speak a short line. Long text is auto-chunked at sentence boundaries.

    Each chunk is enqueued the moment it finishes synthesizing, so the next
    chunk renders while the previous one is still speaking. A single wait at
    the end keeps consecutive replies from overlapping -- otherwise a second
    reply would talk over the first.
    Silenced entirely while voice is muted (!voice off) -- chat replies still go
    out through their platforms."""
    if not text or not text.strip():
        return
    if is_voice_muted():
        return
    chunks = split_text_for_tts(text)
    if not chunks:
        return
    STATUS_FEED.set_transmission(chunks[0])
    queued_locally = False
    for i, chunk in enumerate(chunks):
        # uuid keeps concurrent replies from colliding on hash(chunk)+index.
        tts_file = os.path.join(os.path.abspath(os.curdir),
                                f"___Cmd{uuid.uuid4().hex}_{i}.wav")
        if not tts.synthesize(chunk, tts_file):
            continue
        audio_manager.enqueue_audio(tts_file, delete_file=True)
        queued_locally = True
    if queued_locally:
        audio_manager.wait_until_idle()


def _speak_event(text, tts, audio_manager):
    speak_line(text, tts, audio_manager)


def handle_streak(username, streak, display_name="", memory_db=None,
                  tts=None, audio_manager=None):
    """Voice recognition for a viewer's 'Watch Streak reached!' reward redemption.
    Speaks only when the new streak exceeds the viewer's previously recorded best,
    then persists the higher mark so the bot remembers it across sessions."""
    if streak <= 0:
        return
    viewer = display_name or username or "viewer"
    best = memory_db.get_streak(username) if memory_db else 0
    if streak <= best:
        return
    if memory_db:
        memory_db.set_streak(username, streak)
    if streak < 3:
        return
    if streak <= 10:
        line = f"{viewer}, your attention to {config.STREAMER_NAME}'s streams has been noted. Thank you for your support."
    elif streak <= 99:
        line = (f"{viewer}, your support for watching {config.STREAMER_NAME}'s streams "
                "has been noted, and is appreciated!")
    elif streak <= 199:
        line = (f"{viewer}, {config.STREAMER_NAME} has been notified that you've held "
                f"the line for {streak} streams!")
    elif streak <= 249:
        line = (f"{viewer}, you've watched {streak} streams and {config.STREAMER_NAME} still "
                "hasn't bought you a drink? Stick around after the stream and we'll "
                "fix that.")
    else:
        line = (f"Holy shit {viewer}, you are dedicated. {streak} streams of showing up - "
                f"{config.STREAMER_NAME} owes you a proper drink and a front-row seat.")
    print(f"[green][Streak] {viewer}: {streak} — shouting out")
    STATUS_FEED.record(f"[Streak] {viewer}: streak {streak}")
    _speak_event(line, tts, audio_manager)


def handle_raid(username, party, display_name="", memory_db=None,
                tts=None, audio_manager=None):
    """Voice the bot's reaction to an incoming Twitch raid. The first raid from a
    streamer triggers a battle-stations alert; later raids from the same streamer
    are treated as friendly (persisted across restarts)."""
    raider = display_name or username or "a streamer"
    party = max(0, int(party or 0))
    first = not (memory_db.has_raided(username) if memory_db else False)
    if first and memory_db:
        memory_db.mark_raided(username)
    if first:
        line = (f"{config.STREAMER_NAME}, {raider} has hit us with a raiding party of {party}. "
                "Everyone look alive until we know if they're friendly. If they're not... "
                "well, I'll do what I do.")
    else:
        line = (f"{config.STREAMER_NAME}, {raider} is back with another raiding party of {party}. "
                "Stand down the alert - they were friendly last time.")
    print(f"[green][Raid] {raider} party={party} first={first}")
    STATUS_FEED.record(f"[Raid] {raider} (party of {party}, {'first' if first else 'repeat'})")
    _speak_event(line, tts, audio_manager)


def _run_async(coro):
    """Synchronously run an async coroutine to completion.

    generate_bot_response is async, but the bot's main loop and
    process_message are synchronous. This tiny bridge creates a dedicated
    event loop per call so the surrounding flow stays unchanged.
    """
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    finally:
        asyncio.set_event_loop(None)


def _resolve_provider(ai_engine):
    """Map the active brain engine to an intel_pipeline provider.

    local = Ollama routing; gemini = Google. Matches core/ai_engine's
    DualEngine/LocalAiEngine/AiEngine selection.
    """
    if ai_engine is None:
        return "gemini"
    if isinstance(ai_engine, LocalAiEngine):
        return "local"
    if isinstance(ai_engine, DualEngine) and getattr(ai_engine, "_using_local", False):
        return "local"
    return "gemini"


def _is_local_engine(ai_engine):
    """True when replies are served by the local Ollama core rather than the Gemini API."""
    return _resolve_provider(ai_engine) == "local"


def _build_tts_engine(name):
    """Build a TTS engine by name. Pocket is the only engine in this build, but
    the indirection keeps TTS_ENGINE a config choice rather than a code edit."""
    key = (name or config.TTS_ENGINE).strip().lower()
    if key == "pocket":
        return PocketTTS(server_url=config.POCKET_SERVER_URL,
                         timeout=config.POCKET_TIMEOUT_S)
    # Unknown names fall back to the configured default rather than dying, so a
    # typo in .env leaves the bot able to speak.
    print(f"[yellow]Unknown TTS_ENGINE '{key}', falling back to "
          f"'{config.TTS_ENGINE}'")
    return PocketTTS(server_url=config.POCKET_SERVER_URL,
                     timeout=config.POCKET_TIMEOUT_S)


def _gemini_budget_guard(ai_engine):
    """Replicate AiEngine.chat's daily budget guards for the pipeline's Gemini
    path (local Ollama is free, so it is skipped). Returns the shutdown reply
    (and sets budget_shutdown_requested) when a cap is hit, else None."""
    gemini = getattr(ai_engine, "gemini", ai_engine)

    remaining = gemini._remaining_tokens() if hasattr(gemini, "_remaining_tokens") else None
    if remaining is not None and remaining <= config.HARD_STOP_TOKENS:
        return gemini._budget_exhausted_reply(
            f"Daily token budget nearly exhausted ({remaining} tokens left). {config.BOT_NAME} is calling it a night.")

    return None


def _local_fallback(text, base_system_prompt, ai_engine, relax=False, history=None):
    """Fall back to the local Ollama core when every Gemini channel is exhausted.

    Mirrors the old DualEngine behaviour: when the Gemini dial is exhausted the
    bot routes the same request to the local model instead of dying.

    history: prior user/assistant turns, so the local core keeps the thread
    instead of answering each message in isolation."""
    local = getattr(ai_engine, "local", None) or (
        ai_engine if isinstance(ai_engine, LocalAiEngine) else None)
    if local is None:
        return ("Gemini's comm budget just ran out and I don't have a local "
                "core dialed. Stand by — I'm riding silent for a moment.")
    print("[yellow]Gemini quota exhausted — falling back to the local core.")
    messages = [{"role": "system", "content": base_system_prompt}]
    for m in history or []:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": text})
    try:
        return local.chat(messages, relax=relax)
    except Exception as e:
        print(f"[red]Local core fallback failed: {e}")
        return None


def _generate_brain_response(text, username, base_system_prompt, ai_engine, relax=False, history=None):
    """Route a user message through core.intel_pipeline.generate_bot_response.

    On the Gemini dial this mirrors the old AiEngine/DualEngine behaviour with
    rate-limit awareness: a per-minute rate-limit (429 rateLimitExceeded) backs
    off and retries the SAME channel, while a daily quota (429 quota) or 503
    overload rotates to the next Gemini frequency. Only when every channel is
    exhausted do we fall back to the local Ollama core. All calls are paced by
    the global throttle. Non-API errors return None (silent)."""
    provider = _resolve_provider(ai_engine)
    if provider == "gemini":
        guard_reply = _gemini_budget_guard(ai_engine)
        if guard_reply:
            return guard_reply
        gemini = getattr(ai_engine, "gemini", ai_engine)
        channels = list(getattr(gemini, "_models", []) or [])
        if not channels:
            channels = [os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite"]
        refresh = getattr(gemini, "_refresh_exhausted", None)
        if refresh:
            refresh()
        memory_db = getattr(gemini, "memory_db", None)
        channel_idx = getattr(gemini, "model_index", 0) or 0
        channel_idx %= len(channels)

        # Predictive daily-RPD gate: drop channels already at their hard-coded
        # cap (or 429-quota'ed earlier this session) so we never burn a call
        # that is guaranteed to 429. If none are left, go straight to local.
        live = [c for c in channels
                if c not in getattr(gemini, "exhausted_models", set())
                and not channel_rpd_exhausted(c, memory_db)]
        if not live:
            print("[red]Every Gemini channel is at its daily RPD cap. "
                  "Falling back to the local core.")
            return _local_fallback(text, base_system_prompt, ai_engine, relax=relax, history=history)

        answer = None
        used_gemini = False
        hard_error = False
        rate_attempts = 0
        channel_rotations = 0
        transport_retries = 0
        consecutive_quota = 0
        while channel_rotations < len(channels):
            current = channels[channel_idx % len(channels)]

            # Skip RPD-capped/exhausted channels instead of trying them.
            if current not in live:
                mark = getattr(gemini, "mark_exhausted", None)
                if mark:
                    mark(current)
                channel_idx = (channel_idx + 1) % len(channels)
                channel_rotations += 1
                continue

            try:
                coro = generate_bot_response(
                    text,
                    username=username or "Chatter",
                    provider="gemini",
                    system_prompt=base_system_prompt,
                    model_name=current,
                    history=history,
                )
                answer = _run_async(coro)
                used_gemini = True
                break
            except Exception as e:
                from google.genai import errors as genai_errors
                kind = classify_gemini_error(e)
                # classify_gemini_error returns None for unexpected errors that
                # aren't actionable (non-API, non-transport, non-retryable). For
                # a genuine API/transport problem we still want to move on, so a
                # None kind on a real genai APIError is treated as a hard error
                # below; anything else that's not classifiable is re-raised so a
                # bug in our own code isn't silently swallowed.
                if kind is None and not isinstance(e, genai_errors.APIError) \
                        and not (getattr(e.__class__, "__module__", "").startswith("httpx")
                                 or getattr(e.__class__, "__module__", "").startswith("httpcore")):
                    raise
                if kind is None:
                    print(f"[red]Gemini API error ({getattr(e, 'code', None) or ''}): {e}")
                    hard_error = True
                    answer = None
                    break
                if kind == "rate_limit":
                    rate_attempts += 1
                    if rate_attempts >= config.GEMINI_RATE_RETRIES:
                        print(f"[yellow]{current} still rate-limited after {rate_attempts} "
                              "retries. Rotating to the next channel.")
                        rotate = getattr(gemini, "rotate_channel", None)
                        if rotate:
                            rotate()
                        channel_idx = (channel_idx + 1) % len(channels)
                        channel_rotations += 1
                        rate_attempts = 0
                    else:
                        delay = backoff_delay(rate_attempts)
                        print(f"[yellow]{current} rate-limited (RPM/TPM). Backing off "
                              f"{delay:.1f}s and retrying the same channel...")
                        time.sleep(delay)
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
                # dead model name (404) -> skip & rotate, don't abort the reply
                if kind == "bad_model":
                    print(f"[red]Channel {current} is not served (404) — marking "
                          "it dead for this session and rotating.")
                    mark = getattr(gemini, "mark_exhausted", None)
                    if mark:
                        mark(current)
                    rotate = getattr(gemini, "rotate_channel", None)
                    if rotate:
                        rotate()
                    channel_idx = (channel_idx + 1) % len(channels)
                    channel_rotations += 1
                    continue
                # Quota is per project, not per channel: two channels in a row
                # quota-429 means the daily cap is gone; stop grinding through
                # channels (which just adds throttle/pacing delay) and fall
                # back to the local core.
                if kind == "quota":
                    consecutive_quota += 1
                    if consecutive_quota >= 2:
                        print("[red]Gemini daily request quota exhausted "
                              "(project-wide) — every channel is 429. "
                              "Falling back to the local core.")
                        answer = _local_fallback(
                            text, base_system_prompt, ai_engine, relax=relax,
                            history=history)
                        break
                else:
                    consecutive_quota = 0
                # quota / overload -> rotate to next channel
                reason = "under high demand (503)" if kind == "overload" else "out of quota (429)"
                print(f"[yellow]Channel {current} is {reason}. Rotating comm frequencies "
                      f"(attempt {channel_rotations + 1}/{len(channels)})...")
                rotate = getattr(gemini, "rotate_channel", None)
                if rotate:
                    rotate()
                channel_idx = (channel_idx + 1) % len(channels)
                channel_rotations += 1
                if channel_rotations >= len(channels):
                    print("[red]All Gemini channels exhausted. Falling back to local core.")
                    answer = _local_fallback(text, base_system_prompt, ai_engine, relax=relax,
                                             history=history)
                    break
                time.sleep(10)

        # The loop can exit without an answer when every channel was skipped by
        # the RPD/exhaustion gate (no exception raised). Fall back to local then.
        if not answer and not used_gemini and not hard_error:
            answer = _local_fallback(text, base_system_prompt, ai_engine, relax=relax, history=history)

        if answer and used_gemini:
            memory_db = getattr(gemini, "memory_db", None)
            if memory_db:
                memory_db.add_request_usage(1)
                if hasattr(memory_db, "add_channel_usage"):
                    memory_db.add_channel_usage(current, 1)
        return answer

    # Local dial (free - no budget, no rotation)
    coro = generate_bot_response(
        text,
        username=username or "Chatter",
        provider=provider,
        system_prompt=base_system_prompt,
        history=history,
    )
    return _run_async(coro)


def process_message(text, source="mic", username=None, chatter_memory=None, knowledge_base=None,
                    conversation=None, ai_engine=None, tts=None, audio_manager=None, stream_tool=None,
                    memory_db=None, memory_users=None,
                    twitch_listener=None, reply_via_twitch=False,
                    discord_bridge=None, discord_channel_id=None,
                    obs_overlay=None,
                    lore_manager=None, ooc_mode=None):
    if not text.strip():
        return

    # Community OOC toggle from plain chat (not just !ooc/!ic): any chatter on
    # any platform says "ooc" to break character or "ic" to step back in.
    if ooc_mode:
        bare = text.strip().lower()
        if bare in ("ooc", "ic"):
            new_state, line = ooc_mode.attempt_toggle(username or "Chatter")
            if line:
                if new_state is not None:
                    STATUS_FEED.record(f"[OOC] {username or 'Chatter'} -> {('OOC' if new_state else 'Character')}")
                    speak_line(line, tts, audio_manager)
                elif config.OOC_ENABLED:
                    speak_line(line, tts, audio_manager)
            return

    flood = ai_engine.check_flood(source) if ai_engine else None
    if flood == "":
        return
    if isinstance(flood, str) and flood and source == "chat":
        print(f"[red]Comms flooded — dropping message from {username}")
        speak_line(flood, tts, audio_manager)
        return

    knowledge_context = ""
    t_start = time.time()
    t_play = 0.0
    if knowledge_base:
        knowledge_context = knowledge_base.context_for_query(text, config.KNOWLEDGE_TOP_K)

    chatter_context = ""
    if chatter_memory and username:
        chatter_context = chatter_memory.get_context_string(username)

    memory_context = ""
    if memory_db and username:
        memory_context = memory_db.context_block(username, limit=config.MEMORY_TOP_K)
    elif memory_db and memory_users:
        memory_context = memory_db.context_block_multi(memory_users, per_user=config.MEMORY_GROUP_PER_USER)

    # Entity lore: recognized collab partners / allied personalities. Count the
    # encounter so the lore drifts (trust warm-up) before the reply is built.
    lore_context = ""
    if lore_manager and username and not _is_streamer(username):
        lore_manager.observe_encounter(username)
        entry = lore_manager.get_entity(username)
        # LLM-driven lore extraction: once a chatter crosses the scan threshold
        # (and isn't already characterized), build an auto-learned profile from
        # what they've been observed saying. Costs one request, guarded to fire once.
        if (entry and not entry.get("auto_learned")
                and int(entry.get("encounters", 0)) >= config.LORE_AUTO_SCAN_AT
                and ai_engine is not None):
            lore_manager.classify_and_learn(username, ai_engine, chatter_memory)
        lore_context = lore_manager.get_chatter_context(username)
    elif lore_manager and memory_users:
        lore_context = lore_manager.get_chatter_contexts_multi(memory_users)

    # OOC mode: a global, community-toggleable out-of-character mode. When
    # active we swap in the OOC system prompt (dropping the persona) but keep
    # knowledge + memories so the bot stays useful and remembers people. We also
    # slide a transition marker into history so the model doesn't bleed persona
    # across turns when the mode flips.
    ooc_active = bool(ooc_mode and ooc_mode.is_ooc())
    if ooc_mode:
        ooc_mode.touch()
        was_ooc = getattr(ooc_mode, "_last_was_ooc", False)
        ooc_mode._last_was_ooc = ooc_active
        if ooc_active != was_ooc:
            marker = ("MODE: OOC — prior turns were in-fiction. Answer out of "
                      "character from here." if ooc_active
                      else f"MODE: CHARACTER — resume the {config.BOT_NAME} persona now.")
            conversation.chat_history.append({
                "role": "system",
                "content": marker,
            })

    base_prompt = OOC_SYSTEM_PROMPT if ooc_active else BOT_SYSTEM_PROMPT
    base_content = base_prompt["content"]
    if knowledge_context:
        base_content += f"\n\n{knowledge_context}"
    if chatter_context:
        base_content += f"\n\n{chatter_context}"
    if memory_context:
        base_content += f"\n\n{memory_context}"
    if lore_context and not ooc_active:
        base_content += f"\n\n{lore_context}"
    # The bot's own facts (favorites, canon history). Personal rather than
    # per-viewer, so this is identical for every prompt. Skipped in OOC mode
    # alongside viewer lore, since OOC drops the persona entirely.
    if not ooc_active:
        from core.bot_facts import render_facts
        _facts = render_facts()
        if _facts:
            base_content += f"\n\n{_facts}"

    conversation.chat_history[0] = {"role": "system", "content": base_content}

    # Snapshot prior turns first: add_user_message appends the current message,
    # and passing it as history too would send it to the model twice.
    history_for_model = conversation.get_history()[1:] if conversation else []
    conversation.add_user_message(text)
    t_brain0 = time.time()
    ai_response = _generate_brain_response(text, username, base_content, ai_engine,
                                           relax=ooc_active, history=history_for_model)
    t_brain = time.time() - t_brain0
    if not ai_response:
        return

    ai_response, learned_facts = extract_memories(ai_response)
    if not ai_response:
        return
    print(f"[cyan]\nBOT [{source}] -> {username or 'Chatter'}:")
    print(f"[white]{ai_response}")
    if learned_facts and memory_db:
        for owner, fact in learned_facts:
            target = owner or username
            if target:
                memory_db.add_memory(target, fact)
            else:
                print(f"[yellow]Unattributed memory skipped: {fact}")

    conversation.add_assistant_message(ai_response)

    if chatter_memory and username:
        chatter_memory.add_message(username, "user", text)
        chatter_memory.add_message(username, "assistant", ai_response)
        chatter_memory.increment_interaction(username)
        chatter_memory.save()

    if reply_via_twitch and twitch_listener and config.TWITCH_TEXT_REPLIES:
        twitch_listener.send(ai_response)

    # Post Discord-origin replies back to Discord (if the message came from Discord).
    if source == "discord" and discord_bridge and discord_channel_id:
        try:
            discord_bridge.send_text(ai_response, channel_id=discord_channel_id)
        except Exception as e:
            print(f"[red]Discord reply failed: {e}")

    speech_chunks = split_text_for_tts(ai_response)
    if speech_chunks:
        STATUS_FEED.set_transmission(speech_chunks[0])
        tag = "OOC" if ooc_active else source
        STATUS_FEED.record(f"[{tag}] {ai_response[:500]}")
    voice_on = not is_voice_muted()
    t_tts = 0.0
    if voice_on and stream_tool:
        stream_tool.on_ai_speaking()
    if voice_on and obs_overlay:
        try:
            obs_overlay.on_ai_speaking()
        except Exception:
            pass
    if voice_on:
        for i, chunk in enumerate(speech_chunks):
            tts_file = os.path.join(os.path.abspath(os.curdir), f"___Msg{hash(ai_response)}{i}.wav")
            t_s0 = time.time()
            ok = tts.synthesize(chunk, tts_file)
            t_tts += time.time() - t_s0
            if not ok:
                continue
            t_p0 = time.time()
            audio_manager.play_audio(tts_file, delete_file=True)
            t_play += time.time() - t_p0

    if voice_on and stream_tool:
        stream_tool.on_ai_done()
    if voice_on and obs_overlay:
        try:
            obs_overlay.on_ai_done()
        except Exception:
            pass

    STATUS_FEED.set_latency(
        total_ms=(time.time() - t_start) * 1000.0,
        brain_ms=t_brain * 1000.0,
        tts_ms=t_tts * 1000.0,
        play_ms=t_play * 1000.0,
        source=source,
    )

    if getattr(ai_engine, "budget_shutdown_requested", False):
        print(f"[red]Daily token budget reached — {config.BOT_NAME} is down for the night. Shutting down.")
        time.sleep(2)
        os._exit(0)

    print("[green]\n!!!!!!!\nFINISHED PROCESSING DIALOGUE.\nREADY FOR NEXT INPUT\n!!!!!!!\n")


def local_input_loop(local_queue):
    while True:
        try:
            text = input()
            if text.strip():
                local_queue.put(text.strip())
        except (EOFError, OSError):
            break


def _drain_dashboard_cmds():
    """Return commands left by the LCARS dashboard (clicked buttons), draining
    the file. Each command is run by the caller exactly as if the streamer had
    typed it at the console, so all normal per-command guards apply. Accepts a
    single {"cmd": ...} payload or a list of them (queued button mashing).
    Empty/unset = no dashboard commands."""
    path = getattr(config, "DASHBOARD_CMD_FILE", "") or ""
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    try:
        os.remove(path)
    except OSError:
        pass
    raw = payload if isinstance(payload, list) else [payload]
    cmds = []
    for item in raw:
        cmd = ""
        if isinstance(item, dict):
            cmd = item.get("cmd", "")
        cmd = cmd.strip() if isinstance(cmd, str) else ""
        if cmd:
            cmds.append(cmd)
    return cmds


def cleanup_stale_tts_files():
    """Remove leftover TTS wav files from a previous crash/kill."""
    removed = 0
    for name in os.listdir("."):
        if name.startswith("___Cmd") or name.startswith("___Msg"):
            try:
                os.remove(name)
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[yellow]Cleaned {removed} leftover TTS file(s) from a previous session")


def _print_review_alerts():
    """Reserved for scheduled startup reminders. No reminders are configured in
    this distribution."""
    return


def _write_bot_pid():
    """Record this process's PID so start_bot.bat stop can find it.

    A PID file beats scanning the process table: taskkill /IM python.exe would
    be far too broad (it would also hit the Pocket server and the dashboard).
    """
    path = config.PID_FILE
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError as e:
        print(f"[yellow]Could not write PID file: {e}")


def main():
    print(f"[green]Initializing {config.BOT_NAME} AI Voice Assistant...")
    _write_bot_pid()
    cleanup_stale_tts_files()
    _print_review_alerts()

    conversation = ConversationManager(token_limit=config.TOKEN_LIMIT, max_turns=config.CHAT_HISTORY_TURNS)
    chatter_memory = ChatterMemory(config.CHATTER_MEMORY_FILE)
    memory_db = MemoryDB(config.MEMORY_DB_FILE)
    lore_manager = ViewerLoreManager() if config.LORE_ENABLED else None
    if lore_manager:
        print(f"[green]Entity lore loaded: {len(lore_manager.list_all())} entities")
    ooc_mode = OocMode(enabled=config.OOC_ENABLED)
    if config.OOC_ENABLED:
        print("[green]OOC mode armed (say 'ooc' to break character, 'ic' to return)")
    if config.LOCAL_LLM_ENABLED:
        if os.environ.get("GEMINI_API_KEY"):
            ai_engine = DualEngine(memory_db=memory_db)
            print(f"[green]Dual-engine mode: channels 1-{len(ai_engine.gemini._models)} Gemini, "
                  f"channel {len(ai_engine._models)} = {ai_engine.local.model_name} (local). "
                  f"Default: {ai_engine.current_model()}")
        else:
            ai_engine = LocalAiEngine(memory_db=memory_db)
    elif config.GEMINI_API_KEY:
        ai_engine = AiEngine(memory_db=memory_db)
    else:
        print("[red]No brain configured - nothing can answer chat.")
        print("[yellow]Set GEMINI_API_KEY in .env for the cloud model, or set")
        print("[yellow]LOCAL_LLM_ENABLED=1 and run Ollama for a local model.")
        return

    # Ollama keeps a model resident for OLLAMA_KEEP_ALIVE (60m) after the bot
    # exits, so closing the program used to leave the model pinned in VRAM.
    # Release on the way out; a few seconds of delay is irrelevant this late.
    # Also frees anything the user loaded by hand, since this asks Ollama what
    # is resident.
    def _release_vram_on_exit():
        try:
            unloaded, _errors = ai_engine.unload_all()
            if unloaded:
                print(f"[cyan]Shutdown: released {', '.join(unloaded)} from VRAM.")
        except Exception:
            pass

    atexit.register(_release_vram_on_exit)
    migrated = memory_db.import_from_chatter_json(config.CHATTER_MEMORY_FILE)
    if migrated:
        print(f"[green]Migrated {migrated} facts from old JSON memory into SQLite")
    knowledge_base = KnowledgeBase(config.KNOWLEDGE_BASE_FILE, config.TFID_INDEX_FILE,
                                   domain_map_file=config.DOMAIN_MAP_FILE)
    video_processor = VideoProcessor(
        knowledge_base,
        chunk_size=config.KNOWLEDGE_CHUNK_SIZE,
        chunk_overlap=config.KNOWLEDGE_CHUNK_OVERLAP,
        whisper_model=config.WHISPER_MODEL,
        memory_db=memory_db,
    )
    audio_manager = AudioManager(config.AUDIO_DEVICE)
    mic_input = None
    if os.path.exists(config.VOSK_MODEL_PATH):
        try:
            mic_input = MicInput(config.VOSK_MODEL_PATH, device=config.MIC_DEVICE)
        except Exception as e:
            print(f"[yellow]Microphone disabled: {e}")
    else:
        print(f"[yellow]No Vosk model at {config.VOSK_MODEL_PATH}.")
        print("[yellow]Microphone input disabled. Download a model from "
              "https://alphacephei.com/vosk/models to enable it.")
    stream_tool = init_stream_tool()

    chat_message_queue = queue.Queue()
    twitch_listener = None

    local_queue = queue.Queue()
    threading.Thread(target=local_input_loop, args=(local_queue,), daemon=True).start()

    mic_text_queue = queue.Queue()

    def make_passive():
        return PassiveListener(
            mic_input, config.WAKE_WORDS, active_window=config.WAKE_ACTIVE_WINDOW,
            on_text=lambda t: mic_text_queue.put(t),
            busy_fn=lambda: audio_manager.is_audible,
        )

    passive = make_passive() if (config.PASSIVE_LISTEN and mic_input) else None
    if passive:
        passive.start()
    print("[cyan]You can also type commands/messages here in the console and press Enter")

    if config.TWITCH_OAUTH_TOKEN:
        twitch_listener = TwitchChatListener(
            bot_name=config.TWITCH_BOT_NICK,
            channel=config.TWITCH_CHANNEL,
            oauth_token=config.TWITCH_OAUTH_TOKEN,
            command_prefix=config.TWITCH_COMMAND_PREFIX,
            mention_name=config.TWITCH_MENTION_NAME,
            message_queue=chat_message_queue,
        )
        twitch_listener.start()
    else:
        print("[yellow]No TWITCH_OAUTH_TOKEN set. Twitch chat integration disabled.")
        print("[yellow]To enable, set TWITCH_OAUTH_TOKEN in .env")

    # --- Optional: Discord bridge (sharing\Discord) ---
    discord_bridge = None
    discord_message_queue = queue.Queue()
    if config.DISCORD_BOT_TOKEN and config.DISCORD_TEXT_CHANNEL_IDS:
        try:
            discord_mod = _load_sibling_module(
                "kick_discord_bridge",
                os.path.join(DISCORD_KIT, "input", "discord_bot.py"),
            )
            DiscordBridge = getattr(discord_mod, "DiscordBridge", None) if discord_mod else None
            if DiscordBridge is None:
                raise ImportError("DiscordBridge class not found")
        except ImportError as e:
            print(f"[yellow]Discord bridge unavailable: {e}")
            discord_bridge = None
        else:

            def _discord_on_text(display, formatted, channel_id=None):
                """Called by DiscordBridge on the gateway thread. Enqueue so the
                main loop processes it (keeps Discord's gateway responsive)."""
                raw = formatted.split("]: ", 1)[-1] if "]:" in formatted else formatted
                username = f"discord:{display.lower()}"
                discord_message_queue.put(("chat", raw, username, channel_id))

            discord_bridge = DiscordBridge(
                token=config.DISCORD_BOT_TOKEN,
                allowed_channel_ids=config.DISCORD_TEXT_CHANNEL_IDS,
                mention_name=config.TWITCH_MENTION_NAME or "Bot Name",
                command_prefix=config.TWITCH_COMMAND_PREFIX or "!ask",
                owner_name=config.DISCORD_OWNER_NAME,
                ignored_users=config.IGNORED_USERS,
                on_text=_discord_on_text,
                vc_listen=config.DISCORD_VC_LISTEN if hasattr(config, "DISCORD_VC_LISTEN") else False,
                voice_model=mic_input.model if (config.DISCORD_VC_LISTEN and mic_input) else None,
                vc_wake_words=config.WAKE_WORDS,
                vc_active_window=config.WAKE_ACTIVE_WINDOW,
                vc_wake_arm=config.WAKE_PARTIAL_ARM,
            )
            discord_bridge.start()
            print(f"[green]Discord bridge online ({len(config.DISCORD_TEXT_CHANNEL_IDS)} channel(s))")
    else:
        print("[yellow]No DISCORD_BOT_TOKEN / DISCORD_TEXT_CHANNEL_IDS. Discord integration disabled.")

    # --- Optional: OBS WebSocket overlay (sharing\OBS Websocket) ---
    obs_overlay = None
    if config.OBS_ENABLED:
        try:
            obs_mod = _load_sibling_module(
                "kick_obs_overlay",
                os.path.join(OBS_KIT, "input", "obs_overlay.py"),
            )
            ObsStaticOverlay = getattr(obs_mod, "ObsStaticOverlay", None) if obs_mod else None
            if ObsStaticOverlay is None:
                raise ImportError("ObsStaticOverlay class not found")
        except ImportError as e:
            print(f"[yellow]OBS overlay unavailable: {e}")
            obs_overlay = None
        else:
            obs_overlay = ObsStaticOverlay(
                host=config.OBS_HOST,
                port=config.OBS_PORT,
                password=config.OBS_PASSWORD,
                scene=config.OBS_SCENE,
                source=config.OBS_SOURCE,
                enabled=True,
                timeout_s=config.OBS_TIMEOUT_S,
                hide_on_connect=config.OBS_HIDE_ON_CONNECT,
            )
            if not obs_overlay.connect():
                print("[yellow]OBS not reachable at startup; overlay will retry on next speech.")
            else:
                print(f"[green]OBS overlay online (scene '{config.OBS_SCENE}', source '{config.OBS_SOURCE}')")

    # --- Kick mailbox path (shared with sharing\Kick bridge) ---
    kick_mailbox = config.KICK_MAILBOX_PATH

    tts = LazyTTS(lambda: _build_tts_engine(config.TTS_ENGINE))
    print(f"[cyan]Voice engine ({config.TTS_ENGINE}) loading in background...")

    print("[green]\nStarting the loop, press F4 to begin (or type in Twitch chat)")
    print("[cyan]Type !help for available commands\n")

    STATUS_FEED.bind(
        ai_engine=ai_engine,
        memory_db=memory_db,
        passive=passive,
        twitch_connected=twitch_listener is not None,
        discord_connected=discord_bridge is not None,
        obs_enabled=bool(obs_overlay is not None and getattr(obs_overlay, "connected", False)),
        kick_enabled=config.KICK_ENABLED,
        ooc_mode=ooc_mode,
        stream_tool=stream_tool,
    )
    STATUS_FEED.start()
    from core.twitch_live import start as _start_twitch_live
    _start_twitch_live()
    print("[cyan]Status feed online (dashboard: run dashboard.bat)")

    pending_chat = []
    CHAT_BATCH_SIZE = max(1, config.CHAT_BATCH_SIZE)
    print(f"[cyan]Chat batching: {config.BOT_NAME} replies after {CHAT_BATCH_SIZE} chat messages (or instantly for you)")

    timer_line_active = False
    last_timer_text = ""

    def clear_chat_timer():
        nonlocal timer_line_active, last_timer_text
        if timer_line_active:
            sys.stdout.write("\r" + " " * len(last_timer_text) + "\r")
            sys.stdout.flush()
            timer_line_active = False
            last_timer_text = ""

    def maybe_ears_command(cmd_text, username, origin, cid=0):
        """Handles !ears on|off|restart|status. Returns True when consumed."""
        parts = cmd_text.strip().lstrip("!").split()
        if not parts or parts[0].lower() != "ears":
            return False
        if not _is_streamer(username):
            return True
        nonlocal passive
        sub = parts[1].lower() if len(parts) > 1 else "status"
        spoken = None
        if mic_input is None and sub != "status":
            spoken = "Microphone input is disabled - no Vosk model is loaded."
            print(f"[yellow]{spoken}")
            speak_line(spoken, tts, audio_manager)
            return True
        if sub in ("off", "pause", "stop"):
            if passive and passive.pause():
                spoken = "Ears off — I'm not listening anymore."
                print("[yellow]Passive listening paused")
            elif passive and passive.state == "paused":
                spoken = "My ears are already off."
            else:
                spoken = "Passive listening isn't running."
        elif sub in ("on", "start", "resume"):
            if passive and passive.state == "paused":
                passive.resume()
                spoken = "Ears back on, listening for my name."
            elif passive and passive.state in ("running", "starting"):
                spoken = "My ears are already on."
            else:
                passive = make_passive()
                ok = passive.start()
                spoken = "Ears on, listening for my name." if ok else \
                         "No wake words configured — check WAKE_WORDS."
        elif sub == "restart":
            if passive is None:
                passive = make_passive()
            ok = passive.restart()
            spoken = "Ears rebooted, fresh and listening." if ok else \
                     "Couldn't restart — check WAKE_WORDS."
            print("[cyan]Passive listener restarted")
        elif sub == "status":
            spoken = passive.status_line() if passive else \
                     f"Ears are stopped (PASSIVE_LISTEN={'0' if not config.PASSIVE_LISTEN else '1'}). Wake words: {', '.join(config.WAKE_WORDS) or '(none)'}"
        else:
            spoken = "Usage: !ears on | off | restart | status"
        if spoken:
            print(f"[cyan]{spoken}")
            speak_line(spoken, tts, audio_manager)
        return True

    def maybe_voice_command(cmd_text, username, origin, cid=0):
        """Handles !voice on|off|status — streamer-only mute for the bot's vocals.
        While off, it still replies in Twitch chat but stays silent. Returns True
        when consumed."""
        parts = cmd_text.strip().lstrip("!").split()
        if not parts or parts[0].lower() != "voice":
            return False
        if not _is_streamer(username):
            return True
        sub = parts[1].lower() if len(parts) > 1 else "status"
        if sub in ("off", "mute", "silent", "quiet"):
            if is_voice_muted():
                print("[cyan]I'm already in silent mode.")
                return True
            set_voice_muted(True)
            print("[yellow]Voice muted — silent mode. I'll reply in chat only.")
            STATUS_FEED.record("[Voice] Muted — silent mode (chat replies only)")
            # Turning OFF deliberately does not speak: we're going silent.
            return True
        if sub in ("on", "unmute", "sound"):
            if not is_voice_muted():
                spoken = "My voice is already on."
            else:
                set_voice_muted(False)
                spoken = "Voice back on."
                print("[green]Voice on — vocals restored.")
                STATUS_FEED.record("[Voice] Unmuted — vocals on")
            speak_line(spoken, tts, audio_manager)
            return True
        if sub == "status":
            spoken = "Silent mode is on — I'll reply in chat only." if is_voice_muted() \
                else "Voice is on — I can speak normally."
            print(f"[cyan]{spoken}")
            speak_line(spoken, tts, audio_manager)
            return True
        print("[cyan]Usage: !voice on | off | status")
        speak_line("Usage: voice on, off, or status.", tts, audio_manager)
        return True

    def maybe_mute_command(cmd_text, username, origin, cid=0):
        """Handle !mute twitch-chat|twitch-live|all <on|off|status>
        — streamer-only per-platform toggles for the LCARS dashboard. Twitch
        chat and Twitch live-tracking mute independently; dashboard-authored
        commands always get through (the cmd file is not gated)."""
        parts = cmd_text.strip().lstrip("!").split()
        if not parts or parts[0].lower() != "mute":
            return False
        if not _is_streamer(username):
            return True
        target = parts[1].lower() if len(parts) > 1 else "status"
        sub = parts[2].lower() if len(parts) > 2 else "status"

        def say(spoken):
            print(f"[cyan]{spoken}")
            speak_line(spoken, tts, audio_manager)

        if target == "status" or sub == "status":
            statuses = [
                f"Twitch chat: {'OFF' if is_chat_muted('twitch') else 'ON'}",
                f"Twitch LIVE tracking: {'OFF' if not twitch_live_tracking_enabled() else 'ON'}",
            ]
            say("Mute status — " + "; ".join(statuses))
            return True

        valid = ("twitch-chat", "twitch-live", "all")
        if target not in valid:
            say("Usage: !mute twitch-chat | twitch-live | all <on|off>")
            return True
        if sub not in ("on", "off"):
            say(f"Usage: !mute {target} on | off")
            return True

        off = sub == "off"
        labels = []
        if target in ("twitch-chat", "all"):
            set_chat_muted("twitch", off)
            labels.append("Twitch chat")
        if target in ("twitch-live", "all"):
            twitch_live_set_enabled(not off)
            labels.append("Twitch LIVE tracking")
        names = ", ".join(labels) or target
        say(f"{names} {'OFF' if off else 'ON'}")
        STATUS_FEED.record(f"[Mute] {names} -> {'OFF' if off else 'ON'}")
        return True

    def maybe_topic_command(cmd_text, username, origin, cid=0):
        """Handles !topic / !topics / !forget_domain. Returns True when consumed."""
        parts = cmd_text.strip().lstrip("!").split(maxsplit=1)
        verb = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if verb not in ("topic", "topics", "forget_domain"):
            return False

        def reply(msg):
            print(f"[cyan]{msg}")

        if verb == "topics":
            counts = knowledge_base.list_domains()
            scope = knowledge_base.domain_scope
            lines = [f"Knowledge domains (active lens: {', '.join(scope) if scope else 'ALL'}):"]
            for name, c in sorted(counts.items()):
                marker = "*" if scope and name in scope else ("-" if not scope else "")
                lines.append(f"  {marker}{name}: {c['entries']} entries, {c['chunks']} chunks")
            reply("\n".join(lines))
            return True

        is_streamer = _is_streamer(username)
        if not is_streamer:
            return True

        if verb == "topic":
            target = arg.lower() or "all"
            if target in ("all", "*", "everything", "none", "off"):
                knowledge_base.set_domain_scope(None)
                reply("Knowledge lens reset — I'll search everything again.")
                speak_line("Knowledge lens reset. Searching all my training.", tts, audio_manager)
                return True
            if knowledge_base.set_domain_scope(target):
                reply(f"Knowledge lens set to '{target}'. Ask away.")
                speak_line(f"Focusing my memory on {target}.", tts, audio_manager)
            else:
                known = ", ".join(sorted(knowledge_base.domains.keys())) or "(none defined)"
                reply(f"Unknown domain '{target}'. Known: {known}")
            return True

        # forget_domain
        if not arg:
            reply("Usage: !forget_domain <name>")
            return True
        removed = knowledge_base.forget_domain(arg.lower())
        spoken = f"Forgot domain {arg}: {removed} entries removed." if removed \
            else f"No entries found for domain '{arg}'."
        reply(spoken)
        speak_line(spoken, tts, audio_manager)
        return True

    def _lore_reply(msg, origin, cid):
        print(f"[cyan]{msg}")

    def maybe_lore_command(cmd_text, username, origin, cid=0):
        """Handles !lore (entity-lore management). Streamer-only; returns True
        when consumed."""
        parts = cmd_text.strip().lstrip("!").split(maxsplit=2)
        verb = parts[0].lower() if parts else ""
        if verb != "lore":
            return False
        if not lore_manager:
            _lore_reply("Entity lore is disabled (LORE_ENABLED=0).", origin, cid)
            return True
        is_streamer = _is_streamer(username)
        if not is_streamer:
            return True

        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2].strip() if len(parts) > 2 else ""
        usage = ("Usage: !lore | !lore <name> | !lore add <name> <note> | "
                 "!lore trust <name> <0-100> | !lore reset <name>")

        if not sub:
            entities = lore_manager.list_all()
            if not entities:
                _lore_reply("No entity lore stored yet.", origin, cid)
                return True
            names = [
                f"• {e.get('display_name', k)} ({str(e.get('entity_type', 'human')).upper()}, "
                f"trust {e.get('trust_score', 50)}/{100}, "
                f"{e.get('encounters', 0)} encounters"
                + (f", aka {', '.join(e.get('aliases', []))}" if e.get("aliases") else "")
                + ")"
                for k, e in entities
            ]
            _lore_reply(f"Entity lore ({len(names)}):\n" + "\n".join(names), origin, cid)
            return True

        if sub == "add":
            if not arg:
                _lore_reply("Usage: !lore add <name> <note>", origin, cid)
                return True
            name, _, note = arg.partition(" ")
            if not note.strip():
                _lore_reply("Usage: !lore add <name> <note>", origin, cid)
                return True
            lore_manager.add_manual_note(name, note)
            spoken = f"Logging entity lore on {name}."
            _lore_reply(spoken, origin, cid)
            speak_line(spoken, tts, audio_manager)
            return True

        if sub == "trust":
            if not arg:
                _lore_reply("Usage: !lore trust <name> <0-100>", origin, cid)
                return True
            name, _, score = arg.rpartition(" ")
            if not score.isdigit():
                _lore_reply("Usage: !lore trust <name> <0-100>", origin, cid)
                return True
            value = max(0, min(100, int(score)))
            if lore_manager.set_trust(name, value):
                spoken = f"Trust score for {name} set to {value}."
                _lore_reply(spoken, origin, cid)
                speak_line(spoken, tts, audio_manager)
            else:
                _lore_reply("Couldn't set that trust score.", origin, cid)
            return True

        if sub == "reset":
            if not arg:
                _lore_reply("Usage: !lore reset <name>", origin, cid)
                return True
            if lore_manager.reset_auto(arg):
                spoken = f"Cleared auto-learned lore for {arg}."
                _lore_reply(spoken, origin, cid)
                speak_line(spoken, tts, audio_manager)
            else:
                _lore_reply(f"No lore entry for '{arg}'.", origin, cid)
            return True

        if sub == "scan":
            if not arg:
                _lore_reply("Usage: !lore scan <name> — analyze a chatter's recent "
                            "messages into an auto-learned entity profile.", origin, cid)
                return True
            if ai_engine is None:
                _lore_reply("AI engine unavailable — can't auto-classify.", origin, cid)
                return True
            _lore_reply(f"Scanning '{arg}'... this costs one request.", origin, cid)
            band = lore_manager.classify_and_learn(arg, ai_engine, chatter_memory)
            if band:
                spoken = f"Logged a personality read on {arg} ({band})."
            else:
                spoken = f"Couldn't build a personality read on {arg} — not enough signal or the call failed."
            _lore_reply(spoken, origin, cid)
            speak_line(spoken, tts, audio_manager)
            return True

        # Single-entity lookup: any other single word is treated as a name.
        entry = lore_manager.get_entity(sub)
        if not entry:
            _lore_reply(f"No lore for '{sub}'. {usage}", origin, cid)
            return True
        display = entry.get("display_name") or sub
        affil = entry.get("affiliated_streamer") or "-"
        srcs = {n.get("source") for n in entry.get("notes", [])}
        alias_txt = f" | aliases: {', '.join(entry.get('aliases', []))}" if entry.get("aliases") else ""
        lines = [
            f"{display} — {str(entry.get('entity_type', 'human')).upper()} | "
            f"affiliated: {affil} | trust {entry.get('trust_score', 50)}/100 | "
            f"encounters {entry.get('encounters', 0)} | sources: {', '.join(sorted(srcs)) or '-'}{alias_txt}",
        ]
        for note in entry.get("notes", []):
            lines.append(f"  · ({note.get('source', 'seed')}) {note['text']}")
        _lore_reply("\n".join(lines), origin, cid)
        return True

    def maybe_ooc_command(cmd_text, username, origin, cid=0):
        """!ooc / !ic — community toggle for OOC mode (any chatter, cooldown-
        guarded). Returns True when consumed. Bare 'ooc'/'ic' are handled in
        process_message so plain chat can toggle too."""
        if not ooc_mode:
            return False
        t = cmd_text.strip().lower()
        if t in ("!ooc", "!ic"):
            new_state, line = ooc_mode.attempt_toggle(username or "Chatter")
            if line:
                if new_state is not None:
                    STATUS_FEED.record(f"[OOC] {username or 'Chatter'} -> {('OOC' if new_state else 'Character')}")
                    speak_line(line, tts, audio_manager)
                else:
                    speak_line(line, tts, audio_manager)
            return True
        return False

    def drain(q):
        while True:
            try:
                yield q.get_nowait()
            except queue.Empty:
                return

    def dispatch(cmd, user, origin, cid=0):
        return (maybe_ears_command(cmd, user, origin, cid)
                or maybe_voice_command(cmd, user, origin, cid)
                or maybe_mute_command(cmd, user, origin, cid)
                or maybe_topic_command(cmd, user, origin, cid)
                or maybe_lore_command(cmd, user, origin, cid)
                or maybe_ooc_command(cmd, user, origin, cid))

    def special(text, username, cid=0):
        return handle_special_command(text, username, chatter_memory, video_processor,
                                      knowledge_base, conversation, memory_db=memory_db,
                                      ai_engine=ai_engine)

    def drain_self_input(q, source, display, origin):
        for text in drain(q):
            if dispatch(text, config.STREAMER_NAME, origin):
                continue
            spoken = special(text, config.STREAMER_NAME)
            if spoken is False:
                process_message(
                    f"[{config.STREAMER_NAME} via {display}]: {text}", source=source,
                    username=config.STREAMER_NAME,
                    chatter_memory=chatter_memory, knowledge_base=knowledge_base,
                    conversation=conversation, ai_engine=ai_engine,
                    tts=tts, audio_manager=audio_manager, stream_tool=stream_tool,
                    memory_db=memory_db, obs_overlay=obs_overlay,
                    lore_manager=lore_manager, ooc_mode=ooc_mode,
                )
            elif isinstance(spoken, str) and spoken:
                speak_line(spoken, tts, audio_manager)

    while True:
        # ---- Dashboard buttons (partly-interactive GUI) ----
        for db_cmd in _drain_dashboard_cmds():
            print(f"[cyan]Dashboard command: {db_cmd}")
            if dispatch(db_cmd, config.STREAMER_NAME, "dashboard"):
                continue
            spoken = special(db_cmd, config.STREAMER_NAME)
            if isinstance(spoken, str) and spoken:
                speak_line(spoken, tts, audio_manager)

        # ---- Priority 1: Streamer (console + microphone) ----
        drain_self_input(local_queue, "console", "console", "console")
        drain_self_input(mic_text_queue, "mic", "microphone", "mic")

        # ---- Priority 2: Twitch chat (shared queue, shared batch) ----
        for item in drain(chat_message_queue):
                msg_type = item[0]
                msg_text = item[1] if len(item) > 1 else ""
                username = item[2] if len(item) > 2 else ""
                extra3 = item[3] if len(item) > 3 else 0
                extra4 = item[4] if len(item) > 4 else 0
                if is_chat_muted("twitch"):
                    continue
                if msg_type == "streak":
                    handle_streak(username, int(extra3 or 0), extra4 or username,
                                  memory_db=memory_db, tts=tts, audio_manager=audio_manager)
                    continue
                if msg_type == "raid":
                    handle_raid(username, int(extra3 or 0), extra4 or username,
                                memory_db=memory_db, tts=tts, audio_manager=audio_manager)
                    continue
                if msg_type == "chat":
                    bare = username
                    streamer_names = {config.STREAMER_TWITCH_NAME.lower()}
                    if bare.strip().lower() in streamer_names and f"'{username}'" in msg_text:
                        msg_text = msg_text.replace(f"'{username}'", f"'{config.STREAMER_NAME}'", 1)
                        username = config.STREAMER_NAME

                    cmd_text = msg_text
                    if cmd_text.startswith("[Twitch viewer '"):
                        cmd_text = cmd_text.split("]: ", 1)[1] if "]: " in cmd_text else cmd_text

                    if dispatch(cmd_text, username, "twitch"):
                        continue

                    spoken = special(cmd_text, username)
                    if spoken is False:
                        if _is_streamer(username):
                            print(f"[magenta]{config.STREAMER_NAME} request — responding immediately...")
                            process_message(
                                msg_text, source="chat", username=username,
                                chatter_memory=chatter_memory, knowledge_base=knowledge_base,
                                conversation=conversation, ai_engine=ai_engine,
                                tts=tts, audio_manager=audio_manager, stream_tool=stream_tool,
                                memory_db=memory_db,
                                twitch_listener=twitch_listener,
                                reply_via_twitch=True,
                                obs_overlay=obs_overlay,
                                lore_manager=lore_manager,
                                ooc_mode=ooc_mode,
                            )
                        else:
                            pending_chat.append((time.time(), msg_text, username))
                    elif isinstance(spoken, str) and spoken:
                        print(f"[magenta]Command response to {username}...")
                        speak_line(spoken, tts, audio_manager)

        # ---- Priority 2b: Discord chat (queued by DiscordBridge.on_text) ----
        for item in drain(discord_message_queue):
            msg_type = item[0] if len(item) > 0 else ""
            msg_text = item[1] if len(item) > 1 else ""
            username = item[2] if len(item) > 2 else ""
            channel_id = item[3] if len(item) > 3 else 0
            if is_chat_muted("twitch"):
                # Discord shares the same mute gate as Twitch chat input.
                continue
            cmd_text = msg_text
            if dispatch(cmd_text, username, "discord", channel_id):
                continue
            spoken = special(cmd_text, username, channel_id)
            if spoken is False:
                if _is_streamer(username):
                    print(f"[magenta]{config.STREAMER_NAME} Discord request — responding immediately...")
                    process_message(
                        msg_text, source="discord", username=username,
                        chatter_memory=chatter_memory, knowledge_base=knowledge_base,
                        conversation=conversation, ai_engine=ai_engine,
                        tts=tts, audio_manager=audio_manager, stream_tool=stream_tool,
                        memory_db=memory_db,
                        discord_bridge=discord_bridge, discord_channel_id=channel_id,
                        obs_overlay=obs_overlay,
                        lore_manager=lore_manager, ooc_mode=ooc_mode,
                    )
                else:
                    # Discord messages are answered one-at-a-time (no batching),
                    # since Discord's gateway is real-time.
                    process_message(
                        msg_text, source="discord", username=username,
                        chatter_memory=chatter_memory, knowledge_base=knowledge_base,
                        conversation=conversation, ai_engine=ai_engine,
                        tts=tts, audio_manager=audio_manager, stream_tool=stream_tool,
                        memory_db=memory_db,
                        discord_bridge=discord_bridge, discord_channel_id=channel_id,
                        obs_overlay=obs_overlay,
                        lore_manager=lore_manager, ooc_mode=ooc_mode,
                    )
            elif isinstance(spoken, str) and spoken:
                print(f"[magenta]Command response to {username}...")
                speak_line(spoken, tts, audio_manager)

        # ---- Priority 2c: Kick chat (shared file mailbox with sharing\Kick) ----
        if config.KICK_ENABLED and kick_mailbox and os.path.exists(kick_mailbox):
            try:
                with open(kick_mailbox, "r", encoding="utf-8") as f:
                    kick_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                kick_data = {"inbox": [], "outbox": []}

            kick_inbox = kick_data.get("inbox", [])
            kick_data["inbox"] = []  # clear before processing to avoid re-reads

            for item in kick_inbox:
                msg_type = item.get("type", "chat")
                msg_text = item.get("message", "")
                username = item.get("username", "KickViewer")
                msg_id = item.get("id", "")
                if not msg_text:
                    continue
                if is_chat_muted("twitch"):
                    continue
                cmd_text = msg_text
                if dispatch(cmd_text, username, "kick", msg_id):
                    continue
                spoken = special(cmd_text, username, msg_id)
                if spoken is False:
                    if _is_streamer(username):
                        print(f"[magenta]{config.STREAMER_NAME} Kick request — responding immediately...")
                        process_message(
                            msg_text, source="kick", username=f"kick:{username}",
                            chatter_memory=chatter_memory, knowledge_base=knowledge_base,
                            conversation=conversation, ai_engine=ai_engine,
                            tts=tts, audio_manager=audio_manager, stream_tool=stream_tool,
                            memory_db=memory_db,
                            obs_overlay=obs_overlay,
                            lore_manager=lore_manager, ooc_mode=ooc_mode,
                        )
                    else:
                        process_message(
                            msg_text, source="kick", username=f"kick:{username}",
                            chatter_memory=chatter_memory, knowledge_base=knowledge_base,
                            conversation=conversation, ai_engine=ai_engine,
                            tts=tts, audio_manager=audio_manager, stream_tool=stream_tool,
                            memory_db=memory_db,
                            obs_overlay=obs_overlay,
                            lore_manager=lore_manager, ooc_mode=ooc_mode,
                        )
                elif isinstance(spoken, str) and spoken:
                    print(f"[magenta]Command response to {username}...")
                    speak_line(spoken, tts, audio_manager)

            # Write back the mailbox (inbox cleared; outbox preserved for the Kick bridge).
            try:
                tmp = kick_mailbox + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(kick_data, f, indent=2)
                os.replace(tmp, kick_mailbox)
            except OSError as e:
                print(f"[yellow]Could not write Kick mailbox: {e}")

        # The local core answers each message as soon as it lands: it has no
        # RPD, so batching only makes the streamer wait. Cloud channels stay
        # batched to spread quota across replies. Computed before the
        # pending_chat guard because the countdown below reads it.
        immediate = config.CHAT_IMMEDIATE_ON_LOCAL and _is_local_engine(ai_engine)

        if pending_chat:
            oldest = pending_chat[0][0]
            batch_ready = len(pending_chat) >= CHAT_BATCH_SIZE
            timed_out = time.time() - oldest >= config.CHAT_FLUSH_SECONDS
            if immediate or batch_ready or timed_out:
                clear_chat_timer()
                # Immediate mode still drains the whole buffer, so messages
                # that landed during this same tick ride along in one reply.
                raw_batch = pending_chat[:CHAT_BATCH_SIZE]
                del pending_chat[:len(raw_batch)]
                batch = [item for _, item, _ in raw_batch]
                combined = "\n".join(batch)
                batch_users = []
                for t in batch:
                    m = re.search(r"\[Twitch viewer '([^']+)'", t)
                    if m and m.group(1) not in batch_users:
                        batch_users.append(m.group(1))
                who = ", ".join(batch_users)
                print(f"[magenta]Processing {len(batch)} chat messages ({who})...")
                # A one-chatter batch can be attributed safely. Multi-user batches
                # stay anonymous rather than attributing facts to the wrong person.
                batch_username = batch_users[0] if len(batch_users) == 1 else None
                process_message(
                    combined, source="chat", username=batch_username,
                    chatter_memory=chatter_memory, knowledge_base=knowledge_base,
                    conversation=conversation, ai_engine=ai_engine,
                    tts=tts, audio_manager=audio_manager, stream_tool=stream_tool,
                    memory_db=memory_db, memory_users=batch_users,
                    twitch_listener=twitch_listener, reply_via_twitch=True,
                    obs_overlay=obs_overlay,
                    lore_manager=lore_manager,
                    ooc_mode=ooc_mode,
                )

        if config.CHAT_REPLY_TIMER and not _is_local_engine(ai_engine) and pending_chat and not immediate and not batch_ready and not timed_out:
            secs_left = max(0, int(config.CHAT_FLUSH_SECONDS - (time.time() - oldest)))
            text = f"\r[Chat] {config.BOT_NAME} replies in {secs_left // 60}:{secs_left % 60:02d} | {len(pending_chat)}/{CHAT_BATCH_SIZE} msgs buffered   "
            sys.stdout.write(text)
            sys.stdout.flush()
            timer_line_active = True
            last_timer_text = text
        elif timer_line_active and not pending_chat:
            clear_chat_timer()

        if keyboard.is_pressed("f4"):
            while keyboard.is_pressed("f4"):
                time.sleep(0.05)
            if not mic_input:
                print("[red]Microphone input is disabled (no Vosk model).")
                continue
            print("[green]User pressed F4 key! Now listening to your microphone:")
            try:
                mic_result = mic_input.listen_continuous()
            except Exception as e:
                print(f"[red]Microphone error: {e}")
                mic_result = ""
            if mic_result:
                process_message(
                    f"[{config.STREAMER_NAME} via microphone]: {mic_result}", source="mic",
                    username=config.STREAMER_NAME,
                    chatter_memory=chatter_memory, knowledge_base=knowledge_base,
                    conversation=conversation, ai_engine=ai_engine,
                    tts=tts, audio_manager=audio_manager, stream_tool=stream_tool,
                    memory_db=memory_db, obs_overlay=obs_overlay,
                    lore_manager=lore_manager,
                    ooc_mode=ooc_mode,
                )
            else:
                print("[red]Did not receive any input from your microphone!")

        time.sleep(0.1)


if __name__ == "__main__":
    main()
