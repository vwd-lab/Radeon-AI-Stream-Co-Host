import json
import os
import re
import shutil
import time
from datetime import datetime
from urllib.parse import urlparse

import config
from rich import print

try:
    import joblib
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False
    print("[yellow]scikit-learn not installed. Knowledge base retrieval disabled.")


DEFAULT_DOMAINS = {
    "example": {
        "hosts": ["example.com", "en.wikipedia.org"],
        "keywords": ["example", "wikipedia"],
    },
}


class KnowledgeBase:
    def __init__(self, knowledge_file, tfidf_file, domain_map_file=None):
        self.knowledge_file = knowledge_file
        self.tfidf_file = tfidf_file
        self.domain_map_file = domain_map_file
        self.domains = {}
        self.domain_scope = None  # None = all domains; list[str] = filtered
        self.data = {"videos": {}, "chunks": [], "stats": {"total_videos": 0, "total_chunks": 0, "total_words": 0}}
        self.vectorizer = None
        self.tfidf_matrix = None
        self.load()
        self.load_domain_map()
        self.assign_domains_retroactive()

    # ---------- domain machinery ----------

    def load_domain_map(self):
        if self.domain_map_file and os.path.exists(self.domain_map_file):
            try:
                with open(self.domain_map_file, "r", encoding="utf-8") as f:
                    self.domains = json.load(f)
                print(f"[green]Loaded domain map: {len(self.domains)} domain(s)")
                return
            except (json.JSONDecodeError, IOError):
                print("[yellow]Could not load domain map, using defaults")
        self.domains = json.loads(json.dumps(DEFAULT_DOMAINS))
        if self.domain_map_file:
            self.save_domain_map()

    def save_domain_map(self):
        try:
            os.makedirs(os.path.dirname(self.domain_map_file), exist_ok=True)
            with open(self.domain_map_file, "w", encoding="utf-8") as f:
                json.dump(self.domains, f, indent=2, ensure_ascii=False)
        except OSError as e:
            print(f"[yellow]Could not save domain map: {e}")

    @staticmethod
    def _host_of(url):
        try:
            host = (urlparse(url).netloc or "").lower()
            return host[4:] if host.startswith("www.") else host
        except Exception:
            return ""

    def domain_for_url(self, url):
        host = self._host_of(url or "")
        for name, spec in self.domains.items():
            if host in [h.lower() for h in spec.get("hosts", [])]:
                return name
        return None

    def assign_domains_retroactive(self):
        """One-time pass: tag every entry missing a domain using its URL host.
        VODs/videos without a known host land in 'streams'/'general'."""
        changed = 0
        for vid_id, entry in self.data["videos"].items():
            if entry.get("domain"):
                continue
            source = entry.get("source", "")
            domain = self.domain_for_url(entry.get("url", ""))
            if not domain:
                domain = "streams" if source in ("twitch_vod", "youtube", "video") else "general"
            entry["domain"] = domain
            changed += 1
        if changed:
            self.save()
            print(f"[green]Domain tagging: sorted {changed} existing entries into domains")

    def set_domain_scope(self, names):
        """names: None (= all), a string, or a list of domain names."""
        if names is None or names == "all" or names == []:
            self.domain_scope = None
            return True
        if isinstance(names, str):
            names = [names]
        valid = [n for n in names if n in self.domains]
        if not valid:
            return False
        self.domain_scope = valid
        return True

    def auto_domains_for_query(self, query):
        """Keyword lens: returns domain names whose keywords appear in the
        query, or None when nothing matches (= search everything)."""
        q = (query or "").lower()
        hits = []
        for name, spec in self.domains.items():
            for kw in spec.get("keywords", []):
                if kw.lower() in q:
                    hits.append(name)
                    break
        return hits or None

    def list_domains(self):
        counts = {}
        for vid_id, entry in self.data["videos"].items():
            d = entry.get("domain", "general")
            counts.setdefault(d, {"entries": 0, "chunks": 0})
            counts[d]["entries"] += 1
            counts[d]["chunks"] += entry.get("chunk_count", 0)
        return counts

    def forget_domain(self, name):
        victims = [vid for vid, e in self.data["videos"].items() if e.get("domain") == name]
        if not victims:
            return 0
        del_set = set(victims)
        for vid in victims:
            self.data["videos"].pop(vid, None)
        self.data["chunks"] = [c for c in self.data["chunks"] if c["video_id"] not in del_set]
        self._update_stats()
        self.save()
        self.rebuild_index()
        print(f"[green]Forgot domain '{name}': {len(victims)} entr(y/ies) removed")
        return len(victims)

    # ---------- storage ----------

    def load(self):
        if os.path.exists(self.knowledge_file):
            try:
                with open(self.knowledge_file, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                # Report real counts, not the stored stats block: a merge can
                # leave stats describing only one side, which reads as data loss.
                print(f"[green]Loaded knowledge base: {len(self.data.get('videos', {}))} videos, "
                      f"{len(self.data.get('chunks', []))} chunks")
            except (json.JSONDecodeError, IOError):
                print("[yellow]Could not load knowledge base, starting fresh")

        if HAS_SKLEARN and os.path.exists(self.tfidf_file):
            try:
                self.vectorizer, self.tfidf_matrix = joblib.load(self.tfidf_file)
                print("[green]Loaded TF-IDF index")
            except Exception:
                print("[yellow]Could not load TF-IDF index, will rebuild")

    KB_BACKUP_KEEP = 8
    KB_BACKUP_MIN_INTERVAL_S = 3600

    def save(self):
        """Atomically replace the KB, then refresh a rotating backup.

        The previous version opened the destination and json.dump()'d straight
        into it, so an interrupted write left a truncated or zero-filled file
        -- which is how the 103MB knowledge base was destroyed on 2026-10-20.
        Writing to a temp file and os.replace()-ing leaves the destination
        either fully old or fully new, never partial.
        """
        target = self.knowledge_file
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self._rotate_backup()

    def _rotate_backup(self):
        """Copy the just-written KB into data/kb_backups/, keeping the newest KB_BACKUP_KEEP.

        Throttled to one copy per KB_BACKUP_MIN_INTERVAL_S: add_video() calls
        save() once per page, and the corpus is ~100MB, so an unthrottled
        backup would copy the whole file once per page trained.
        """
        try:
            bdir = os.path.join(os.path.dirname(self.knowledge_file), "kb_backups")
            os.makedirs(bdir, exist_ok=True)
            names = sorted(n for n in os.listdir(bdir)
                           if n.startswith("kb_") and n.endswith(".json"))
            if names:
                newest = os.path.join(bdir, names[-1])
                if time.time() - os.path.getmtime(newest) < self.KB_BACKUP_MIN_INTERVAL_S:
                    return
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            dst = os.path.join(bdir, f"kb_{stamp}.json")
            shutil.copy2(self.knowledge_file, dst)
            # copy2 preserves the source mtime, which would make the throttle
            # above measure the KB's age instead of when we last backed it up.
            os.utime(dst, None)
            names = sorted(n for n in os.listdir(bdir)
                           if n.startswith("kb_") and n.endswith(".json"))
            for stale in names[:-self.KB_BACKUP_KEEP]:
                try:
                    os.remove(os.path.join(bdir, stale))
                except OSError:
                    pass
            print(f"[green]KB backup kept: {names[-1]}")
        except OSError as e:
            print(f"[yellow]KB backup failed: {e}")

    def save_tfidf(self):
        if self.vectorizer is not None and self.tfidf_matrix is not None:
            os.makedirs(os.path.dirname(self.tfidf_file), exist_ok=True)
            joblib.dump((self.vectorizer, self.tfidf_matrix), self.tfidf_file)

    def add_video(self, video_id, title, source, url, chunks_text, metadata=None):
        domain = (metadata or {}).get("domain") or self.domain_for_url(url)
        if not domain:
            domain = "streams" if source in ("twitch_vod", "youtube", "video") else "general"
        self.data["videos"][video_id] = {
            "title": title,
            "source": source,
            "url": url,
            "added_at": datetime.now().isoformat(),
            "duration_seconds": metadata.get("duration_seconds", 0) if metadata else 0,
            "chunk_count": len(chunks_text),
            "tags": metadata.get("tags", []) if metadata else [],
            "domain": domain,
        }
        # MediaWiki revision id this text was read at. Absent for non-wiki
        # sources, and for wiki pages trained before !wikirefresh existed.
        revid = (metadata or {}).get("wiki_revid")
        if revid:
            self.data["videos"][video_id]["wiki_revid"] = revid

        for i, chunk_text in enumerate(chunks_text):
            chunk_id = f"{video_id}_chunk_{i}"
            self.data["chunks"].append({
                "id": chunk_id,
                "video_id": video_id,
                "text": chunk_text,
                "chunk_index": i,
                "total_chunks": len(chunks_text),
                "source_type": source,
                "video_title": title,
            })

        self._update_stats()
        self.save()
        self.rebuild_index()
        print(f"[green]Added '{title}' to knowledge base ({len(chunks_text)} chunks)")

    def stage_video(self, video_id, title, source, url, chunks_text, metadata=None):
        """Replace a video's text/chunks IN MEMORY only.

        Deliberately does not save() or rebuild_index(): both cost ~95MB of
        TF-IDF work on the current corpus, so calling them per page would make
        a bulk refresh unusable. Bulk callers must call save() and
        rebuild_index() once when they are done.
        """
        if video_id in self.data["videos"]:
            self.data["chunks"] = [c for c in self.data["chunks"]
                                   if c["video_id"] != video_id]
        else:
            self.data["chunks"] = list(self.data["chunks"])
        domain = (metadata or {}).get("domain") or self.domain_for_url(url)
        if not domain:
            domain = "streams" if source in ("twitch_vod", "youtube", "video") else "general"
        entry = {
            "title": title,
            "source": source,
            "url": url,
            "added_at": datetime.now().isoformat(),
            "duration_seconds": (metadata or {}).get("duration_seconds", 0),
            "chunk_count": len(chunks_text),
            "tags": (metadata or {}).get("tags", []),
            "domain": domain,
        }
        revid = (metadata or {}).get("wiki_revid")
        if revid:
            entry["wiki_revid"] = revid
        self.data["videos"][video_id] = entry
        for i, text in enumerate(chunks_text):
            self.data["chunks"].append({
                "id": f"{video_id}_chunk_{i}",
                "video_id": video_id,
                "text": text,
                "chunk_index": i,
                "total_chunks": len(chunks_text),
                "source_type": source,
                "video_title": title,
            })
        self._update_stats()

    def remove_video(self, video_id):
        if video_id not in self.data["videos"]:
            return False
        del self.data["videos"][video_id]
        self.data["chunks"] = [c for c in self.data["chunks"] if c["video_id"] != video_id]
        self._update_stats()
        self.save()
        self.rebuild_index()
        print(f"[green]Removed video {video_id} from knowledge base")
        return True

    def rebuild_index(self):
        if not HAS_SKLEARN or not self.data["chunks"]:
            self.vectorizer = None
            self.tfidf_matrix = None
            return

        texts = [c["text"] for c in self.data["chunks"]]
        self.vectorizer = TfidfVectorizer(
            max_features=10000,
            stop_words="english",
            ngram_range=(1, 2),
        )
        self.tfidf_matrix = self.vectorizer.fit_transform(texts)
        self.save_tfidf()
        print(f"[green]Rebuilt TF-IDF index ({len(texts)} chunks)")

    def _chunk_domain(self, chunk):
        entry = self.data["videos"].get(chunk.get("video_id"), {})
        return entry.get("domain", "general")

    def retrieve(self, query, top_k=5, domains=None):
        if not HAS_SKLEARN or self.vectorizer is None or self.tfidf_matrix is None:
            return self._fallback_retrieve(query, top_k, domains)
        if not self.data["chunks"]:
            return []

        effective = self.domain_scope if domains is None else (domains if isinstance(domains, list) else [domains])
        allowed = set(effective) if effective else None

        query_vec = self.vectorizer.transform([query])
        similarities = cosine_similarity(query_vec, self.tfidf_matrix).flatten()
        order = similarities.argsort()[::-1]

        results = []
        floor = getattr(config, "KB_RELEVANCE_FLOOR", 0.25)
        for idx in order:
            if len(results) >= top_k:
                break
            if similarities[idx] <= floor:
                break
            chunk = self.data["chunks"][idx]
            if allowed is not None and self._chunk_domain(chunk) not in allowed:
                continue
            out = chunk.copy()
            out["score"] = float(similarities[idx])
            out["domain"] = self._chunk_domain(chunk)
            results.append(out)
        if not results:
            print(f"[dim]No chunk cleared the relevance floor ({floor:.2f}) "
                  f"— model must answer without knowledge-base help.")
        return results

    def _fallback_retrieve(self, query, top_k, domains=None):
        effective = self.domain_scope if domains is None else (domains if isinstance(domains, list) else [domains])
        allowed = set(effective) if effective else None
        query_words = set(re.findall(r'\w+', query.lower()))
        scored = []
        for chunk in self.data["chunks"]:
            if allowed is not None and self._chunk_domain(chunk) not in allowed:
                continue
            chunk_words = set(re.findall(r'\w+', chunk["text"].lower()))
            overlap = len(query_words & chunk_words)
            if overlap > 0:
                scored.append((overlap, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        out_chunks = []
        for _, c in scored[:top_k]:
            c = c.copy()
            c["domain"] = self._chunk_domain(c)
            out_chunks.append(c)
        return out_chunks

    def format_context(self, query, top_k=5, domains=None):
        results = self.retrieve(query, top_k, domains=domains)
        cue = (getattr(config, "KB_ABSTENTION_TEXT", "")
               if getattr(config, "KB_ABSTENTION_CUE", True) else "")
        if not results:
            # Nothing cleared the floor. Say so explicitly rather than handing
            # the model an empty context and letting it invent an answer.
            return ("NO RELEVANT KNOWLEDGE-BASE NOTES were found for this "
                    "question.\n" + cue) if cue else ""
        lines = ["RELEVANT GAME KNOWLEDGE (from trained videos and websites):"]
        for i, chunk in enumerate(results, 1):
            text = chunk["text"][:300] + "..." if len(chunk["text"]) > 300 else chunk["text"]
            origin = chunk.get("domain", "general")
            lines.append(f'{i}. "{text}" (from: {chunk["video_title"]} [{origin}])')
        if cue:
            lines.append("")
            lines.append(cue)
        return "\n".join(lines)

    def context_for_query(self, query, top_k=5):
        """Pinned scope wins; otherwise keyword lens; otherwise all domains."""
        if self.domain_scope:
            return self.format_context(query, top_k, domains=self.domain_scope)
        auto = self.auto_domains_for_query(query)
        if auto:
            print(f"[dim]Knowledge lens: {', '.join(auto)} (keyword match)")
            return self.format_context(query, top_k, domains=auto)
        return self.format_context(query, top_k)

    def list_videos(self):
        return self.data["videos"]

    def _update_stats(self):
        self.data["stats"]["total_videos"] = len(self.data["videos"])
        self.data["stats"]["total_chunks"] = len(self.data["chunks"])
        self.data["stats"]["total_words"] = sum(
            len(c["text"].split()) for c in self.data["chunks"]
        )
