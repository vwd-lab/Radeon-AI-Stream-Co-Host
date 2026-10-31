import time
from rich import print

import config
from training.youtube_extractor import YouTubeExtractor
from training.twitch_vod_extractor import TwitchVodExtractor
from training.chat_replay import ChatReplayDownloader
from training.profile_miner import ProfileMiner
from training.wiki_extractor import WikiExtractor
from training.local_file_extractor import LocalFileExtractor
from training.website_extractor import WebsiteExtractor
from training.text_chunker import chunk_text


class VideoProcessor:
    def __init__(self, knowledge_base, chunk_size=500, chunk_overlap=50, whisper_model="base", memory_db=None):
        self.kb = knowledge_base
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.memory_db = memory_db
        self.youtube_extractor = YouTubeExtractor()
        self.twitch_extractor = TwitchVodExtractor(whisper_model=whisper_model)
        self.local_extractor = LocalFileExtractor(whisper_model=whisper_model)
        self.website_extractor = WebsiteExtractor()
        self.chat_replay = ChatReplayDownloader()
        self.profile_miner = ProfileMiner(max_per_user=config.PROFILE_MAX_PER_USER)
        self.wiki_extractor = WikiExtractor()

    def train_url(self, url, tags=None):
        url_lower = url.lower()
        if url_lower.startswith("http://") or url_lower.startswith("https://"):
            if "youtube.com" in url_lower or "youtu.be" in url_lower:
                return self._process_youtube(url, tags)
            elif "twitch.tv" in url_lower:
                return self._process_twitch_vod(url, tags)
            else:
                return self._process_website(url, tags)
        elif "." in url_lower and " " not in url_lower:
            return self._process_website(f"https://{url}", tags)
        else:
            print(f"[red]Unsupported URL: {url}")
            return None

    def train_wiki(self, url, max_pages=None, categories=None):
        max_pages = max_pages if max_pages and max_pages > 0 else (
            config.WIKI_MAX_PAGES or None
        )
        known = set(self.kb.data["videos"].keys())
        return self.wiki_extractor.train_site(
            url, self._store_result, max_pages=max_pages, categories=categories,
            is_known_fn=lambda page_id: page_id in known,
        )

    def train_directory(self, dir_path, tags=None):
        results = self.local_extractor.extract_directory(dir_path)
        count = 0
        for result in results:
            if self._store_result(result, tags):
                count += 1
        print(f"[green]Trained on {count}/{len(results)} files from {dir_path}")
        return count

    def _process_youtube(self, url, tags=None):
        result = self.youtube_extractor.extract(url)
        if not result:
            return None
        return self._store_result(result, tags)

    def _process_twitch_vod(self, url, tags=None):
        result = self.twitch_extractor.extract(url)
        if not result:
            return None
        stored = self._store_result(result, tags)
        if config.VOD_CHAT_ENABLED:
            self._store_vod_chat(result["video_id"], tags)
        return stored

    def _store_vod_chat(self, vod_id, tags=None):
        try:
            chat_result, comments = self.chat_replay.extract(
                vod_id, max_comments=config.VOD_CHAT_MAX_COMMENTS,
            )
            if chat_result:
                merged_tags = list(tags or []) + ["vod-chat"]
                self._store_result(chat_result, tags=merged_tags)
            if config.PROFILE_MINING_ENABLED and self.memory_db and comments:
                profiles = self.profile_miner.mine(comments, source_label="from VOD chat")
                saved = 0
                for user, facts in profiles.items():
                    for fact in facts:
                        if self.memory_db.add_memory(user, fact):
                            saved += 1
                if saved:
                    print(f"[green]Saved {saved} new profile fact(s) to long-term memory")
        except Exception as e:
            print(f"[red]Chat replay training failed (VOD itself still trained): {e}")

    def _process_website(self, url, tags=None):
        result = self.website_extractor.extract(url)
        if not result:
            return None
        return self._store_result(result, tags)

    def _store_result(self, result, tags=None):
        if not result or not result.get("text"):
            print("[red]No text content extracted")
            return None

        video_id = result["video_id"]
        if video_id in self.kb.data["videos"]:
            print(f"[yellow]'{result['title']}' already in knowledge base, skipping")
            return None

        chunks = chunk_text(result["text"], self.chunk_size, self.chunk_overlap)
        if not chunks:
            print("[red]No chunks generated from content")
            return None

        metadata = {
            "duration_seconds": result.get("duration_seconds", 0),
            "tags": tags or [],
        }
        self.kb.add_video(
            video_id=video_id,
            title=result["title"],
            source=result["source"],
            url=result.get("url", ""),
            chunks_text=chunks,
            metadata=metadata,
        )
        return result["title"]

    def refresh_wiki(self, site_url, force=False, contains=None, max_changed=None,
                     categories=None):
        """Re-fetch wiki pages whose content changed since they were trained.

        The normal trainer is additive only: a page's id is a hash of
        host+title, so an edited page is skipped forever. This walks the pages
        already stored for one host, compares each stored `wiki_revid` against
        the wiki's current revision (50 titles per API call), and re-fetches
        only the ones that moved.

        Entries with no stored revid (everything trained before this existed)
        are BACKFILLED with the current revid instead of being re-fetched, so
        the first run is cheap and does not silently re-download a whole site.
        Pass force=True to re-fetch everything on that host anyway.

        `categories` limits the check to pages reachable from those wiki
        categories (recursive, same enumeration as !trainwiki). Pages in those
        categories that were never trained are reported separately -- refresh
        can only update what already exists; new pages need !trainwiki.
        """
        from urllib.parse import urlparse

        extractor = self.wiki_extractor
        api_url, host = extractor.discover_api(site_url)
        if not api_url:
            print("[red]No MediaWiki API found for that site.")
            return {"checked": 0, "changed": 0, "backfilled": 0, "updated": 0}

        host = host.lower()
        stored = []
        for video_id, data in self.kb.data["videos"].items():
            if not video_id.startswith("wiki_"):
                continue
            if urlparse(data.get("url") or "").netloc.lower() != host:
                continue
            stored.append((video_id, data.get("title") or "", data))

        entries = stored
        if contains:
            entries = [e for e in stored if contains.lower() in e[1].lower()]

        if categories:
            print(f"[cyan]Enumerating pages in {len(categories)} category(ies) "
                  f"(this can take a moment)...")
            cat_titles = list(extractor.list_category_titles(api_url, categories))
            wanted = {t.strip().lower() for t in cat_titles}
            entries = [e for e in stored if e[1].strip().lower() in wanted]
            trained = {e[1].strip().lower() for e in entries}
            missing = wanted - trained
            print(f"[cyan]{len(wanted)} page(s) in those categories; "
                  f"{len(entries)} already trained")
            if missing:
                shown = ", ".join(sorted(missing)[:8])
                more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
                print(f"[yellow]{len(missing)} not trained yet, so refresh cannot "
                      f"update them: {shown}{more}")
                print("[yellow]Use !trainwiki with the same Category: list to add them.")

        scope = f" matching '{contains}'" if contains else ""
        print(f"[cyan]{len(entries)} stored {host} page(s) to check{scope}")
        if not entries:
            return {"checked": 0, "changed": 0, "backfilled": 0, "updated": 0,
                    "not_trained": 0}

        live = extractor.fetch_revisions(api_url, [t for _, t, _ in entries if t])
        print(f"[cyan]Resolved {len(live)} current revision id(s)")

        backfilled = changed = 0
        to_fetch = []
        for video_id, title, data in entries:
            live_revid = live.get(title)
            stored = data.get("wiki_revid")
            if stored is None:
                if live_revid is not None:
                    data["wiki_revid"] = live_revid
                    backfilled += 1
                continue
            if force or live_revid is None or str(live_revid) != str(stored):
                to_fetch.append((video_id, title, live_revid))

        if backfilled:
            print(f"[green]Backfilled revision id on {backfilled} page(s) "
                  f"(no re-fetch; run again later to catch real edits)")
        if not to_fetch:
            print("[green]Nothing changed. Knowledge base is already current.")
            if backfilled:
                self.kb.save()
            return {"checked": len(entries), "changed": 0,
                    "backfilled": backfilled, "updated": 0}

        if max_changed:
            to_fetch = to_fetch[:max_changed]
        print(f"[cyan]{len(to_fetch)} page(s) changed; re-fetching...")

        updated = 0
        session_started = time.time()
        for video_id, title, live_revid in to_fetch:
            page = extractor.fetch_page(api_url, host, title)
            if not page or not page.get("text"):
                print(f"[yellow]Refresh fetch failed for '{title}'; leaving as-is")
                continue
            chunks = chunk_text(page["text"], self.chunk_size, self.chunk_overlap)
            if not chunks:
                continue
            changed += 1
            self.kb.stage_video(
                video_id=video_id,
                title=page.get("title", title),
                source=page.get("source", "mediawiki"),
                url=page.get("url", ""),
                chunks_text=chunks,
                metadata={
                    "duration_seconds": 0,
                    "tags": (self.kb.data["videos"].get(video_id) or {}).get("tags", []),
                    "wiki_revid": live_revid,
                },
            )
            updated += 1
            print(f"[green]Refreshed '{title}' (revid {live_revid})")
            extractor._sleep_with_countdown(
                config.WIKI_SKIP_DELAY, session_started,
                (config.WIKI_TIME_BUDGET or 3600))

        if updated:
            # One save + one index rebuild for the whole batch, not per page.
            self.kb.save()
            self.kb.rebuild_index()
            print(f"[green]Refresh complete: {updated} page(s) updated, "
                  f"{backfilled} backfilled")
        return {"checked": len(entries), "changed": changed,
                "backfilled": backfilled, "updated": updated}

    def forget_video(self, title_or_id):
        for vid_id, video_data in list(self.kb.data["videos"].items()):
            if vid_id == title_or_id or video_data["title"].lower() == title_or_id.lower():
                self.kb.remove_video(vid_id)
                return True
        print(f"[yellow]Video '{title_or_id}' not found in knowledge base")
        return False
