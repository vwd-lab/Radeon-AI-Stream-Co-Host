"""The bot's own personal facts, loaded from data/bot_facts.json.

Separate from core/viewer_lore.py on purpose: that tracks *other* people
(collab partners) with trust scoring, and this tracks the bot itself. Without
this the model improvises answers about its own tastes and contradicts itself
within a single session. These entries are the single source of truth for its
own preferences, and the "rules" block stops it inventing anything not listed.

The file is hand-maintained; restart the bot after editing it.
"""
import json
import os

import config


def _facts_path():
    return getattr(
        config, "BOT_FACTS_FILE",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "bot_facts.json"))


def load_facts(path=None):
    """Return the parsed facts dict, or {} if missing/unreadable.

    Never raises: a malformed file must not stop the bot from starting, it just
    means the bot falls back to improvising (and saying so when asked).
    """
    p = path or _facts_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        print(f"[dim]No bot facts file at {p} - "
              f"personal questions will fall back to 'no record'")
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[yellow]Could not read bot facts file {p}: {exc}")
        return {}


def render_facts(path=None):
    """Formatted block for the system prompt, or '' when disabled/unavailable."""
    if not getattr(config, "BOT_FACTS_ENABLED", True):
        return ""
    data = load_facts(path)
    if not data:
        return ""

    prefs = data.get("personal_preferences") or []
    canon = data.get("canon_backstory") or []
    rules = data.get("rules") or []
    if not prefs and not canon:
        return ""

    lines = ["[THE BOT'S OWN FACTS - authoritative, do not re-improvise these]"]
    if prefs:
        lines.append("Personal preferences:")
        lines.extend(f"- {item}" for item in prefs)
    if canon:
        lines.append("Canon backstory:")
        lines.extend(f"- {item}" for item in canon)
    if rules:
        lines.append("Rules:")
        lines.extend(f"- {item}" for item in rules)
    return "\n".join(lines)
