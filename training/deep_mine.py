import json
import re
from rich import print

import config
from training.twitch_vod_extractor import extract_vod_id
from training.chat_replay import ChatReplayDownloader
from training.profile_miner import is_bot

EXTRACTION_PROMPT = """Below are Twitch chat messages from one VOD, grouped by viewer.
Do two things:

1) Extract personal facts worth remembering about each VIEWER: interests, games they play, favorite things, location, age, occupation, life events, strong opinions about games/streaming.

2) Find notable REFERENCES TO THE STREAMER (the channel owner and their persona): jokes that landed, recurring nicknames viewers use for him, praise or complaints, requests, reactions to his gameplay or stories, memorable moments attributed to him.

RULES:
- Only include what a viewer explicitly stated or made unmistakably clear. No speculation.
- Skip emote spam, memes, reactions, jokes, and sarcasm EXCEPT when the joke itself is about the streamer (those belong in streamer_references).
- Phrase each fact as a short third-person statement (e.g. "plays racing games", "is from Germany"). For streamer references, credit the viewer when clear (e.g. "'dave1' calls the streamer 'Skipper'").
- Maximum 5 items per category entry.
- Return ONLY valid JSON in exactly this shape, no markdown fences, no commentary:
{{"viewers": [{{"username": "...", "facts": ["...", "..."]}}], "streamer_references": ["...", "..."]}}
- Omit empty sections entirely.

CHAT LOG:
{batch}"""


class DeepMiner:
    def __init__(self, ai_engine, memory_db=None, max_comments=15000,
                 users_per_batch=20, min_messages=2, timeout=30, max_batch_chars=8000):
        self.ai = ai_engine
        self.memory_db = memory_db
        self.max_comments = max_comments
        self.users_per_batch = users_per_batch
        self.min_messages = min_messages
        self.max_batch_chars = max_batch_chars
        self.downloader = ChatReplayDownloader(timeout=timeout)

    def _group_by_user(self, comments):
        grouped = {}
        for _, user, text in comments:
            if is_bot(user) or user.lower() in config.IGNORED_USERS or not text.strip():
                continue
            grouped.setdefault(user, []).append(text.strip())
        return {
            user: msgs
            for user, msgs in grouped.items()
            if len(msgs) >= self.min_messages
        }

    def _user_line(self, user, grouped):
        joined = " | ".join(m[:100] for m in grouped[user][:60])
        return f"{user}: {joined}"

    def _plan_batches(self, users, grouped):
        """Split viewers into batches honoring BOTH the per-batch viewer cap
        and the per-call character budget. Every viewer lands in exactly one
        batch — no silent drops."""
        batches, current, used = [], [], 0
        for user in users:
            cost = len(self._user_line(user, grouped))
            if current and (len(current) >= self.users_per_batch or used + cost > self.max_batch_chars):
                batches.append(current)
                current, used = [], 0
            current.append(user)
            used += cost
        if current:
            batches.append(current)
        return batches

    def _format_batch(self, chunk, grouped):
        return "\n".join(self._user_line(user, grouped) for user in chunk)

    def _parse_viewers(self, raw):
        if not raw:
            return [], []
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return [], []
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([\]}])", r"\1", raw[start:end + 1]))
            except json.JSONDecodeError:
                print("[red]DeepMine: could not parse model response")
                return [], []
        if not isinstance(data, dict):
            return [], []
        viewers = data.get("viewers")
        result = []
        if isinstance(viewers, list):
            for entry in viewers:
                if not isinstance(entry, dict):
                    continue
                username = str(entry.get("username") or "").strip()
                facts = [str(f).strip() for f in entry.get("facts") or [] if str(f).strip()]
                if username and facts:
                    result.append((username, facts[:5]))
        streamer_refs = [
            str(r).strip()
            for r in (data.get("streamer_references") or [])
            if isinstance(r, str) and str(r).strip()
        ]
        return result, streamer_refs[:5]

    def mine_vod(self, url_or_id, source_label="from VOD chat"):
        vod_id = extract_vod_id(url_or_id) if "/" in str(url_or_id) else str(url_or_id)
        if not vod_id:
            print(f"[red]DeepMine: could not get VOD ID from: {url_or_id}")
            return 0
        print(f"[yellow]DeepMine: fetching chat replay for VOD {vod_id}...")
        comments = self.downloader.fetch_comments(vod_id, max_comments=self.max_comments)
        if not comments:
            print("[yellow]DeepMine: no chat replay available")
            return 0
        return self.analyze_comments(comments, source_label=source_label)

    def _process_batch_response(self, raw, chunk, source_label):
        """Returns facts saved from one model response. Returns None when the
        API call itself failed (raw is None), so the caller can retry."""
        if raw is None:
            return None
        saved = 0
        found, streamer_refs = self._parse_viewers(raw)
        known = {u.lower() for u in chunk}
        for username, facts in found:
            if username.lower() not in known:
                continue
            for fact in facts:
                full_fact = f"{fact.rstrip('.')} ({source_label})"
                if self.memory_db.add_memory(username, full_fact):
                    saved += 1
        streamer_name = config.STREAMER_NAME
        for ref in streamer_refs:
            full_ref = f"{ref.rstrip('.')} ({source_label})"
            if self.memory_db.add_memory(streamer_name, full_ref):
                saved += 1
        return saved

    def analyze_comments(self, comments, source_label="from VOD chat"):
        print(f"[green]DeepMine: {len(comments)} messages fetched")

        grouped = self._group_by_user(comments)
        users = sorted(grouped, key=lambda u: -len(grouped[u]))
        if not users:
            print("[yellow]DeepMine: no viewers with enough activity to analyze")
            return 0
        planned = self._plan_batches(users, grouped)
        print(f"[cyan]DeepMine: analyzing {len(users)} active viewers in {len(planned)} Gemini call(s)...")

        saved = 0
        failed_batches = []
        for idx, chunk in enumerate(planned, 1):
            prompt = EXTRACTION_PROMPT.format(batch=self._format_batch(chunk, grouped))
            print(f"[dim]DeepMine: batch {idx}/{len(planned)} ({len(chunk)} viewers)...")
            raw = self.ai.chat_single(prompt)
            result = self._process_batch_response(raw, chunk, source_label)
            if result is None:
                print(f"[yellow]DeepMine: batch {idx} failed — queued for retry")
                failed_batches.append((chunk, prompt))
                continue
            saved += result
            print(f"[cyan]DeepMine: batch done, {saved} fact(s) saved so far")

        if failed_batches:
            print(f"[cyan]DeepMine: re-running {len(failed_batches)} failed inquiry/batches...")
            for index, (chunk, prompt) in enumerate(failed_batches, 1):
                raw = self.ai.chat_single(prompt)
                result = self._process_batch_response(raw, chunk, source_label)
                if result is None:
                    print(f"[red]DeepMine: retry {index}/{len(failed_batches)} still failing — batch dropped ({len(chunk)} viewers)")
                    continue
                saved += result
                print(f"[green]DeepMine: retry {index} recovered, {saved} fact(s) saved total")
        print(f"[green]DeepMine complete: {saved} new profile fact(s) stored")
        return saved
