import re
from rich import print

import config

BOT_SUFFIXES = ("bot", "streamelements", "nightbot", "moobot", "fossabot", "wizebot", "streamlabs")

URL_PATTERN = re.compile(r"https?://|www\.|\.(com|net|org|tv|gg)\b", re.IGNORECASE)
REPEAT_SPAM_PATTERN = re.compile(r"^(.{2,30}?)( \1){3,}$", re.IGNORECASE)

FACT_PATTERNS = [
    (re.compile(r"\bmy name is ([a-z0-9_'\- ]{2,25})", re.IGNORECASE),
     lambda m: f"name is {m.group(1).strip().rstrip('.,!?')}"),
    (re.compile(r"\b(?:i'?m|i am) (\d{1,2}) years old\b", re.IGNORECASE),
     lambda m: f"is {m.group(1)} years old"),
    (re.compile(r"\b(?:i'?m|i am) from ([a-z ,]{3,30})", re.IGNORECASE),
     lambda m: f"is from {m.group(1).strip().rstrip('.,!?')}"),
    (re.compile(r"\bi live in ([a-z ,]{3,30})", re.IGNORECASE),
     lambda m: f"lives in {m.group(1).strip().rstrip('.,!?')}"),
    (re.compile(r"\bi(?:'?ve)? been (playing|watching) ([a-z0-9:'\- ]{3,40})", re.IGNORECASE),
     lambda m: f"has been {m.group(1).lower()} {m.group(2).strip().rstrip('.,!?')}"),
    (re.compile(r"\bi (?:main(?:ed)?|play(?:ed)?) ([a-z0-9:'\- ]{3,40})", re.IGNORECASE),
     lambda m: f"plays {m.group(1).strip().rstrip('.,!?')}"),
    (re.compile(r"\bi (love|like|hate) ([a-z0-9:'\- ]{3,40})", re.IGNORECASE),
     lambda m: f"{'hates' if m.group(1).lower() == 'hate' else 'likes'} {m.group(2).strip().rstrip('.,!?')}"),
    (re.compile(r"\bmy favou?rite ([a-z ]{2,20}?) is ([a-z0-9:'\- ]{2,40})", re.IGNORECASE),
     lambda m: f"favorite {m.group(1).strip()} is {m.group(2).strip().rstrip('.,!?')}"),
    (re.compile(r"\bi (work|study) (as|at|in) ((?:an? )?[a-z0-9:'\- ]{2,40})", re.IGNORECASE),
     lambda m: (
         "works" if m.group(1).lower() == "work" else "studies"
     ) + f" {m.group(2).lower()} {m.group(3).strip().rstrip('.,!?')}"),
]

STOPWORDS = ("it", "this", "that", "you", "them", "him", "her", "me too")


def is_bot(username):
    name = username.lower()
    if name in config.IGNORED_USERS:
        return True
    return any(name.endswith(sfx) or name == sfx for sfx in BOT_SUFFIXES)


def extract_fact(text):
    text = text.strip()
    if not (10 <= len(text) <= 120):
        return None
    if URL_PATTERN.search(text) or REPEAT_SPAM_PATTERN.match(text):
        return None
    for pattern, builder in FACT_PATTERNS:
        match = pattern.search(text)
        if match:
            fact = builder(match).strip()
            if not fact or len(fact) < 5:
                continue
            lowered = fact.lower()
            tail = lowered.rsplit(" ", 1)[-1].strip(" .,!?")
            if tail in STOPWORDS or len(tail) < 2:
                continue
            return fact
    return None


class ProfileMiner:
    def __init__(self, max_per_user=3):
        self.max_per_user = max_per_user

    def mine(self, comments, source_label=""):
        profiles = {}
        for _, user, text in comments:
            if is_bot(user):
                continue
            fact = extract_fact(text)
            if not fact:
                continue
            bucket = profiles.setdefault(user, [])
            if any(fact.lower() == existing.lower() for existing in bucket):
                continue
            suffix = f" ({source_label})" if source_label else ""
            bucket.append(fact + suffix)
            if len(bucket) >= self.max_per_user:
                pass
        trimmed = {
            user: facts[: self.max_per_user]
            for user, facts in profiles.items()
            if facts
        }
        if trimmed:
            print(
                f"[green]Profile miner: extracted facts for "
                f"{len(trimmed)} viewer(s): {', '.join(list(trimmed)[:8])}"
            )
        else:
            print("[cyan]Profile miner: no personal facts found in this chat")
        return trimmed
