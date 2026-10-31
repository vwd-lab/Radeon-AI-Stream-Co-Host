import re
import threading
from abc import ABC, abstractmethod

from rich import print

# Pocket has no character ceiling of its own: it renders a whole request at a
# steady RTF ~0.26 and holds quality out past 3,500 chars (measured, no drift).
# This cap exists only so an unusually long reply still arrives in a few pieces
# rather than one huge block.
#
# 3400 is a HARD ceiling, not a target: split_text_for_tts breaks *before*
# appending, so every returned chunk is <= this, never over. The ~100-char gap
# to Pocket's measured 3,500 is deliberate headroom, so a reply landing near the
# boundary still splits a little early instead of relying on Pocket holding up
# exactly to its measured edge.
TTS_MAX_CHARS = 3400


class TextToSpeech(ABC):
    @abstractmethod
    def synthesize(self, text, output_path):
        pass


class LazyTTS:
    def __init__(self, factory):
        self._factory = factory
        self._impl = None
        self._ready = threading.Event()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        self._impl = self._factory()
        self._ready.set()

    @property
    def ready(self):
        return self._ready.is_set()

    @property
    def model(self):
        return self._impl.model if self._impl else None

    def synthesize(self, text, output_path):
        if not self._ready.wait(timeout=600):
            print("[red]TTS engine took too long to load!")
            return None
        return self._impl.synthesize(text, output_path)


def _strip_markdown(text):
    """Remove characters a TTS engine would read literally (asterisks,
    underscores, backticks, hashes) while keeping the words, so the bot never
    stumbles on them."""
    t = re.sub(r"\*\*|__|\*|_|`|~~", "", text)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r" *([,?.!…])", r"\1", t)
    return t.strip()


def split_text_for_tts(text, max_chars=TTS_MAX_CHARS):
    """Split text into TTS-safe chunks (<= max_chars) at sentence boundaries.

    Each chunk is spoken separately. Falls back to comma, then word, then
    hard-cutting so a single huge run-on still gets bounded.
    """
    if len(text) <= max_chars:
        cleaned = _strip_markdown(text)
        return [cleaned] if cleaned else []

    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []

    sentences = re.split(r"(?<=[.!?…])\s+", normalized)

    def _merge(chunks):
        out, cur = [], ""
        for c in chunks:
            if cur and len(cur) + len(c) + 1 > max_chars:
                out.append(cur)
                cur = c
            else:
                cur = f"{cur} {c}".strip() if cur else c
        if cur:
            out.append(cur)
        return out

    grouped = _merge(sentences)
    if all(len(c) <= max_chars for c in grouped):
        return grouped

    final = []
    for group in grouped:
        if len(group) <= max_chars:
            final.append(group)
            continue
        comma_parts = re.split(r"(?<=,)\s+", group)
        for part in _merge(comma_parts):
            if len(part) <= max_chars:
                final.append(part)
                continue
            words = part.split()
            cur = ""
            for w in words:
                if len(w) > max_chars:
                    if cur:
                        final.append(cur)
                        cur = ""
                    for j in range(0, len(w), max_chars):
                        final.append(w[j:j + max_chars])
                    continue
                if cur and len(cur) + len(w) + 1 > max_chars:
                    final.append(cur)
                    cur = w
                else:
                    cur = f"{cur} {w}".strip() if cur else w
            if cur:
                final.append(cur)
    return [_strip_markdown(c) for c in final if c]
