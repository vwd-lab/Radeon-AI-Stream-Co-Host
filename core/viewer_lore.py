import json
import os
import sqlite3
from datetime import datetime

from rich import print

import config


# Optional personality seeds for known collab partners / allied entities.
# These are the source for a recognized partner's first encounter; the bot's
# opinion of them then drifts as they interact (auto-learn layer) and the
# streamer can edit anything by hand in data/viewer_lore.json or via !lore.
#
# Empty by default so a fresh install carries no other people's identities.
# Copy the shape from data/viewer_lore.json to add your own:
#
#   "ally": {
#       "display_name": "Ally",
#       "entity_type": "human",
#       "affiliated_streamer": None,
#       "trust_score": 85,
#       "notes": ["Allied streamer. Reliable, sharp, good banter."],
#   },
SEED_ENTITIES = {}


# Classifier prompt for LLM-driven lore extraction: when an unknown chatter
# becomes a regular, a single quota-aware call (via ai_engine.chat_single)
# turns their observed chat lines into an entity profile auto-learned into
# data/viewer_lore.json (source: auto).
AUTO_LORE_PROMPT = """You are classifying a recurring chatter for the streamer-AI co-host.
A chatter has been seen repeatedly in stream chat. From their messages below,
infer who they likely are so the co-host can treat them accordingly.

Rules:
- Only infer from what is shown. Be conservative: an empty/low-information
  answer is fine (chat often has nothing to work from).
- entity_type: "human" (person/viewer), "bot" (automated assistant / script),
  or "synthetic" (an in-world AI persona). Default "human".
- trust_score: 0-100 for how an ally/nuisance/unknown this contact seems.
  Neutral = 50. Only assign 80+ for clearly helpful/ally-like, <40 for clearly
  hostile/spammy/antagonistic behavior.
- affiliated_streamer: the channel/operator this account appears to belong to,
  if any (leave null unless it's clear).
- assessment: a 1-2 sentence note in the co-host's voice style (dry, cynical,
  military), or an empty string if there is nothing to say.
- If there is genuinely not enough signal, return {{"need_more": true}}.

Return ONLY valid JSON, no markdown fences, no commentary. Shape:
{{"entity_type": "human|bot|synthetic", "trust_score": 50, "affiliated_streamer": null, "assessment": "..."}}

CHATTER: "{username}"
OBSERVED MESSAGES:
{messages}"""


_PLATFORM_PREFIXES = ("twitch:",)

# Default trust score for any viewer who doesn't already have a set trust score
# (new entity slots and viewers hydrating from the DB without a score).
DEFAULT_TRUST = 80


def _normalize(name):
    return (name or "").strip().lower()


def _strip_platform(name):
    key = _normalize(name)
    for prefix in _PLATFORM_PREFIXES:
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    return key


def _trust_band(trust):
    """Map a trust score to the behavioral directive that powers the 'trust
    score must actually affect behavior' decision. The model reads this line
    and treats the contact accordingly."""
    if trust >= 80:
        return ("TRUSTED ALLY",
                "Treat as part of the inner circle: warm, respectful, share openly "
                "and volunteer intel.")
    if trust >= 40:
        return ("NEUTRAL",
                "Professional and polite. Open to collaboration, keep the banter civil.")
    return ("LOW-TRUST",
            "Guarded and terse. Keep replies short, stay professional, and never "
            "volunteer operational secrets.")


class ViewerLoreManager:
    """Entity-lore store for collab partners and allied personalities.

    data/viewer_lore.json is the human-managed source of truth (the streamer can
    edit it by hand or via the !lore commands). Every change is mirrored into a
    SQLite `chatter_memory` table (same shape as the original planning spec) so
    the data stays queryable/durable across writers.
    """

    def __init__(self, lore_file=None, db_path=None):
        self.lore_file = lore_file or config.LORE_FILE
        db_file = db_path or config.MEMORY_DB_FILE
        os.makedirs(os.path.dirname(os.path.abspath(self.lore_file)), exist_ok=True)
        self.conn = sqlite3.connect(db_file)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self._create_table()
        self.viewers = {}
        self._load()
        self._hydrate_from_db()
        self.ensure_seeds()

    # ---- persistence -----------------------------------------------------

    def _create_table(self):
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS chatter_memory (
                username TEXT PRIMARY KEY,
                display_name TEXT,
                entity_type TEXT DEFAULT 'human',
                affiliated_streamer TEXT,
                notes TEXT NOT NULL,
                trust_score INTEGER DEFAULT 50,
                lore_source TEXT DEFAULT 'seed',
                auto_learned INTEGER DEFAULT 0,
                encounters INTEGER DEFAULT 0,
                updated_at TEXT
            )
        ''')
        self.conn.commit()

    def _load(self):
        if not os.path.exists(self.lore_file):
            self.viewers = {}
            return
        try:
            with open(self.lore_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.viewers = data.get("viewers", {}) or {}
        except (json.JSONDecodeError, IOError) as e:
            print(f"[yellow]Could not load {self.lore_file} ({e}); starting fresh.")
            self.viewers = {}

    def _hydrate_from_db(self):
        """Recover from SQLite if the JSON file was lost/cleared."""
        if self.viewers:
            return
        rows = self.conn.execute(
            "SELECT username, display_name, entity_type, affiliated_streamer, notes, "
            "trust_score, auto_learned, encounters FROM chatter_memory"
        ).fetchall()
        for (username, display, etype, affil, notes_json, trust, auto, encounters) in rows:
            try:
                notes_list = json.loads(notes_json) if notes_json else []
            except json.JSONDecodeError:
                notes_list = []
            self.viewers[username] = {
                "display_name": display or username,
                "entity_type": etype or "human",
                "affiliated_streamer": affil,
                "trust_score": int(trust) if trust is not None else DEFAULT_TRUST,
                "notes": notes_list,
                "auto_learned": bool(auto),
                "encounters": int(encounters or 0),
            }
        if self.viewers:
            self.save()

    def ensure_seeds(self):
        """Merge the hardcoded seeds without clobbering existing/manual entries.
        Seed aliases are added to already-existing entries too, so an alias
        shipped in a fresh seed survives a pre-seeded file."""
        changed = False
        for key, seed in SEED_ENTITIES.items():
            if key in self.viewers:
                merged = False
                existing = set(self.viewers[key].get("aliases", []))
                for alias in seed.get("aliases", []):
                    if alias not in existing:
                        self.viewers[key].setdefault("aliases", []).append(alias)
                        merged = True
                if merged:
                    changed = True
                continue
            entry = dict(seed)
            entry["notes"] = [{"text": n, "source": "seed"} for n in seed.get("notes", [])]
            entry["auto_learned"] = False
            entry["encounters"] = 0
            self.viewers[key] = entry
            changed = True
        if changed:
            self.save()

    def save(self):
        folder = os.path.dirname(os.path.abspath(self.lore_file))
        os.makedirs(folder, exist_ok=True)
        payload = {
            "_meta": {
                "description": "Entity-lore cache for collab partners. "
                               "Edit freely; seeds restore only when an entry is absent. "
                               "Sources: seed | manual | auto.",
                "lore_cap": config.LORE_CAP,
            },
            "viewers": self.viewers,
        }
        tmp = self.lore_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.lore_file)
        self._sync_table()

    def _sync_table(self):
        for username, entry in self.viewers.items():
            self.conn.execute('''
                INSERT INTO chatter_memory
                    (username, display_name, entity_type, affiliated_streamer, notes,
                     trust_score, lore_source, auto_learned, encounters, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    display_name=excluded.display_name,
                    entity_type=excluded.entity_type,
                    affiliated_streamer=excluded.affiliated_streamer,
                    notes=excluded.notes,
                    trust_score=excluded.trust_score,
                    lore_source=excluded.lore_source,
                    auto_learned=excluded.auto_learned,
                    encounters=excluded.encounters,
                    updated_at=excluded.updated_at
            ''', (
                username,
                entry.get("display_name", username),
                entry.get("entity_type", "human"),
                entry.get("affiliated_streamer"),
                json.dumps(entry.get("notes", [])),
                int(entry.get("trust_score", 50)),
                "manual" if self._has_manual(entry) else "seed",
                1 if entry.get("auto_learned") else 0,
                int(entry.get("encounters", 0)),
                datetime.now().isoformat(timespec="seconds"),
            ))
        self.conn.commit()

    @staticmethod
    def _has_manual(entry):
        return any(n.get("source") == "manual" for n in entry.get("notes", []))

    # ---- lookup ----------------------------------------------------------

    def _canonical_key(self, username):
        """Resolve a chatter name (including platform-prefixed and aliased
        names) to its canonical lore key, or None if the chatter is unknown.
        Aliases let one person span accounts, e.g. 'Ally' resolves to
        the 'ally_account' entry."""
        key = _strip_platform(username)
        if not key:
            return None
        if key in self.viewers:
            return key
        for k, entry in self.viewers.items():
            for alias in entry.get("aliases", []):
                if _strip_platform(alias) == key:
                    return k
        return None

    def get_entity(self, username):
        key = self._canonical_key(username)
        return self.viewers.get(key) if key else None

    def get_chatter_context(self, username):
        """Formatted tactical snippet for the system prompt, or '' if the
        chatter has no lore. The trust band line makes the score affect how the
        model treats the contact."""
        entry = self.get_entity(username)
        if not entry:
            return ""
        display = entry.get("display_name") or _strip_platform(username)
        trust = int(entry.get("trust_score", 50))
        band, behavior = _trust_band(trust)
        affil_key = entry.get("affiliated_streamer")
        affil = self.viewers.get(affil_key, {}).get("display_name") or affil_key or "-"

        curated = [n["text"] for n in entry.get("notes", []) if n.get("source") != "auto"]
        assessment = " | ".join(curated) if curated else "No assessment logged."

        lines = [
            f"[ENTITY RECOGNIZED: {display}]",
            f"Type: {str(entry.get('entity_type', 'human')).upper()} | "
            f"Affiliated with: {affil} | Encounters: {int(entry.get('encounters', 0))}",
            f"Trust Score: {trust}/100 ({band})",
            f"Behavior directive: {behavior}",
            f"Tactical Assessment: {assessment}",
        ]
        encounters = int(entry.get("encounters", 0))
        if encounters >= config.LORE_AUTO_MIN:
            status = ("warming up after repeated contact" if trust < 50
                      else "established contact")
            lines.append(f"Auto-learned: {display} has been observed {encounters} "
                         f"times across the channels. Status: {status}.")
        return "\n".join(lines)

    def get_chatter_contexts_multi(self, usernames):
        blocks = []
        seen = set()
        for name in usernames:
            canonical = self._canonical_key(name)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            block = self.get_chatter_context(canonical)
            if block:
                blocks.append(block)
        if not blocks:
            return ""
        header = "COLLAB PARTNER INTEL (each section belongs to that specific chatter only):"
        return header + "\n\n" + "\n\n".join(blocks)

    # ---- auto-learn (recognition + drift) --------------------------------

    def observe_encounter(self, username):
        """Drift an entity's lore as they keep showing up. A viewer with no
        lore yet gets a default slot at DEFAULT_TRUST (80); recognized partners
        warm up toward neutral trust (they've earned more rope by returning).
        Anyone with an explicitly set trust score is left to their manager.
        A placeholder slot with no actual personality notes is NOT marked
        auto-learned, so the LLM auto-scan can still characterize it later."""
        entry = self.get_entity(username)
        if not entry:
            _ = self._ensure_entry(username)
            entry = self.get_entity(username)
            if not entry:
                return
        display = entry.get("display_name") or _strip_platform(username)
        encounters = int(entry.get("encounters", 0)) + 1
        entry["encounters"] = encounters
        trust = int(entry.get("trust_score", DEFAULT_TRUST))
        if 0 < trust < 50:
            new_trust = max(0, min(50, trust + config.LORE_TRUST_STEP))
            if new_trust != trust:
                print(f"[magenta]LORE: {display} warmed up (trust {trust} -> {new_trust}).")
                entry["trust_score"] = new_trust
        has_personality = bool(entry.get("notes")) or bool(entry.get("affiliated_streamer"))
        if has_personality and not entry.get("auto_learned"):
            entry["auto_learned"] = True
            print(f"[magenta]LORE: recognized collab partner '{display}' - personality unlocked.")
        self.save()

    # ---- manual management (owner-only via !lore) ------------------------

    def _ensure_entry(self, username, display_name=None):
        key = _strip_platform(username)
        if not key:
            return None
        canonical = self._canonical_key(key)
        if canonical:
            return self.viewers[canonical]
        if key not in self.viewers:
            self.viewers[key] = {
                "display_name": display_name or key,
                "entity_type": "human",
                "affiliated_streamer": None,
                "trust_score": DEFAULT_TRUST,
                "notes": [],
                "auto_learned": False,
                "encounters": 0,
            }
        return self.viewers[key]

    def add_manual_note(self, username, note):
        entry = self._ensure_entry(username)
        if not entry:
            return False
        entry["notes"].append({"text": note.strip(), "source": "manual"})
        manual = [n for n in entry["notes"] if n.get("source") == "manual"]
        if len(manual) > config.LORE_CAP:
            dropped = manual[0]
            entry["notes"].remove(dropped)
            print(f"[yellow]LORE: note cap ({config.LORE_CAP}) hit - dropped oldest: "
                  f"{dropped['text'][:60]}...")
        self.save()
        return True

    def set_trust(self, username, score):
        entry = self._ensure_entry(username)
        if not entry:
            return False
        entry["trust_score"] = max(0, min(100, int(score)))
        self.save()
        return True

    def reset_auto(self, username):
        entry = self.get_entity(username)
        if not entry:
            return False
        entry["auto_learned"] = False
        entry["encounters"] = 0
        entry["notes"] = [n for n in entry.get("notes", []) if n.get("source") != "auto"]
        self.save()
        return True

    # ---- LLM-driven lore extraction (auto-learn discovery) ----------------

    def sample_messages(self, username, chatter_memory, limit=12):
        """Pull up to `limit` recent observed messages for a chatter from the
        legacy ChatterMemory tracker, for feeding the classifier."""
        if not chatter_memory:
            return []
        profile = chatter_memory.get_profile(username)
        recent = profile.get("recent_messages", [])
        out = []
        for m in recent:
            content = (m.get("content") or "").strip()
            if not content:
                continue
            out.append(content)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _parse_auto_classification(raw):
        """Parse the model's JSON into (entity_type, trust, affiliated,
        assessment, need_more). Returns None on parse failure."""
        if not raw:
            return None
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("need_more"):
            return ("human", 50, None, "", True)
        etype = str(data.get("entity_type") or "human").lower()
        if etype not in ("human", "bot", "synthetic"):
            etype = "human"
        try:
            trust = int(data.get("trust_score"))
        except (TypeError, ValueError):
            trust = 50
        trust = max(0, min(100, trust))
        affil = data.get("affiliated_streamer") or None
        assessment = str(data.get("assessment") or "").strip()
        return (etype, trust, affil, assessment, False)

    def classify_and_learn(self, username, ai_engine, chatter_memory=None):
        """LLM-driven lore extraction: turn a recurring chatter into an
        auto-learned entity entry (source: auto). Returns the chosen trust
        band label, or None if the model couldn't classify / the call failed.
        Uses the existing quota-aware single-shot path (ai_engine.chat_single)
        so it honours per-channel RPD caps and channel rotation."""
        if not ai_engine:
            return None
        name = _strip_platform(username)
        if not name:
            return None
        messages = self.sample_messages(name, chatter_memory)
        if not messages:
            print(f"[yellow]LORE: nothing observed for '{name}' to auto-classify.")
            return None
        joined = "\n".join(m[:200] for m in messages[:12])
        prompt = AUTO_LORE_PROMPT.format(username=name, messages=joined)
        try:
            raw = ai_engine.chat_single(prompt)
        except Exception as e:
            print(f"[red]LORE: auto-classify call failed for '{name}': {e}")
            return None
        parsed = self._parse_auto_classification(raw)
        if parsed is None:
            print(f"[yellow]LORE: could not parse auto-classification for '{name}'.")
            return None
        etype, trust, affil, assessment, need_more = parsed
        if need_more:
            print(f"[dim]LORE: not enough signal to classify '{name}' yet.")
            return None
        entry = self._ensure_entry(name)
        if entry.get("auto_learned"):
            print(f"[dim]LORE: '{name}' already auto-learned — skipping.")
            return None
        entry["entity_type"] = etype
        entry["trust_score"] = trust
        entry["affiliated_streamer"] = affil or entry.get("affiliated_streamer")
        entry["auto_learned"] = True
        entry.setdefault("notes", []).append({
            "text": assessment or f"Recurring chatter ({etype}).",
            "source": "auto",
        })
        self.save()
        band, _ = _trust_band(trust)
        display = entry.get("display_name") or name
        print(f"[green]LORE: auto-learned '{display}' as {etype} (trust {trust}, "
              f"band {band}).")
        return band

    def list_all(self):
        return sorted(
            self.viewers.items(),
            key=lambda kv: (int(kv[1].get("trust_score", 0)), kv[0]),
            reverse=True,
        )