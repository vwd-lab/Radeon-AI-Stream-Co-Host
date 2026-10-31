import hashlib
import json
import os
import sys
import time
from urllib.parse import quote, urlparse

from rich import print

import config
from training.website_extractor import DEFAULT_USER_AGENT, STRIP_TAGS


def make_wiki_id(host, title):
    normalized = f"{host.lower()}:{title.strip().lower()}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"wiki_{digest}"


class WikiExtractor:
    def __init__(self, user_agent=DEFAULT_USER_AGENT, timeout=20):
        self.user_agent = user_agent
        self.timeout = timeout
        self.skip_file = getattr(config, "WIKI_SKIP_FILE", None)
        self._skipped_by_host = self._load_skipped()
        self._browser = None

    def _load_skipped(self):
        """Load per-host sets of page_ids skipped in earlier !trainwiki rounds."""
        if not self.skip_file:
            return {}
        try:
            with open(self.skip_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(h): set(str(p) for p in lst) for h, lst in data.items()}
        except (OSError, ValueError):
            return {}

    def _save_skipped(self):
        if not self.skip_file:
            return
        try:
            os.makedirs(os.path.dirname(self.skip_file), exist_ok=True)
            with open(self.skip_file, "w", encoding="utf-8") as f:
                json.dump({h: sorted(s) for h, s in self._skipped_by_host.items()},
                          f, indent=2)
        except OSError as e:
            print(f"[red]Failed to persist wiki skip list: {e}")

    def _get(self, api_url, params):
        if self._browser is not None and self._browser.is_ready(api_url):
            return self._browser.get_json(api_url, params)
        import requests
        try:
            resp = requests.get(
                api_url,
                params={**params, "format": "json"},
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
        except Exception:
            resp = None
        if resp is not None and not self._is_cloudflare_challenge(resp):
            resp.raise_for_status()
            return resp.json()
        if not getattr(config, "WIKI_BROWSER_ENABLED", True):
            if resp is not None:
                resp.raise_for_status()
            raise RuntimeError(f"Wiki API unreachable: {api_url}")
        first_browser_use = self._browser is None
        if first_browser_use:
            print(f"[yellow]Cloudflare challenge on {urlparse(api_url).netloc} — routing API through a real browser...")
        return self._browser_fetch().get_json(api_url, params)

    def _is_cloudflare_challenge(self, resp):
        """True only for a Cloudflare interstitial (403 or a non-JSON
        'Just a moment' body), never for a normal MediaWiki API response."""
        try:
            body = (resp.text or "").lower()
            signature = ("just a moment" in body
                         or "cf-chl" in body
                         or "cf_chl_opt" in body
                         or "cdn-cgi/challenge-platform" in body)
            if not signature:
                return False
            if resp.status_code == 403:
                return True
            content_type = (resp.headers.get("Content-Type", "") or "").lower()
            return "json" not in content_type
        except Exception:
            return False

    def _browser_fetch(self):
        if self._browser is not None:
            return self._browser
        from training.browser_fetch import BrowserFetch
        self._browser = BrowserFetch(
            profile_dir=getattr(config, "WIKI_BROWSER_PROFILE", None) or os.path.join(
                config.DATA_DIR, "wiki_browser_profile"),
            channel=getattr(config, "WIKI_BROWSER_CHANNEL", "msedge"),
            wait_seconds=getattr(config, "WIKI_BROWSER_WAIT", 90),
            timeout_ms=getattr(config, "WIKI_BROWSER_TIMEOUT_MS", 28800),
        )
        return self._browser

    def close_browser(self):
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def discover_api(self, site_url):
        parsed = urlparse(site_url if "://" in site_url else f"https://{site_url}")
        base = f"{parsed.scheme}://{parsed.netloc}"
        candidates = [f"{base}/w/api.php", f"{base}/api.php"]
        last_error = None
        for candidate in candidates:
            try:
                data = self._get(candidate, {"action": "query", "meta": "siteinfo"})
                if data.get("query", {}).get("general"):
                    print(f"[green]MediaWiki API found: {candidate}")
                    return candidate, parsed.netloc
            except Exception as e:
                last_error = e
                continue
        self.close_browser()
        if last_error is not None:
            print(f"[red]Could not reach the MediaWiki API at {base}: {last_error}")
            return None, parsed.netloc
        print(f"[red]No MediaWiki API found at {base} (/w/api.php or /api.php)")
        return None, parsed.netloc

    def list_titles(self, api_url, max_pages=None):
        apfrom = None
        yielded = 0
        while True:
            params = {
                "action": "query",
                "list": "allpages",
                "aplimit": "500",
                "apfilterredir": "nonredirects",
            }
            if apfrom:
                params["apfrom"] = apfrom
            try:
                data = self._get(api_url, params)
            except Exception as e:
                print(f"[red]Wiki page listing failed: {e}")
                return
            pages = (data.get("query") or {}).get("allpages") or []
            for page in pages:
                title = page.get("title")
                if not title:
                    continue
                yield title
                yielded += 1
                if max_pages and yielded >= max_pages:
                    return
            cont = (data.get("continue") or {}).get("apfrom")
            if not cont:
                return
            apfrom = cont

    def list_category_titles(self, api_url, categories, max_pages=None):
        """Yield article titles reachable from the given root categories,
        expanding subcategories recursively. Deduplicated."""
        from queue import Queue
        pending_cats = Queue()
        for name in categories:
            name = name.strip().replace("_", " ")
            if not name:
                continue
            pending_cats.put(name if name.lower().startswith("category:") else f"Category:{name}")
        seen_cats = set()
        seen_titles = set()
        yielded = 0
        while not pending_cats.empty():
            cat = pending_cats.get()
            if cat in seen_cats:
                continue
            seen_cats.add(cat)
            cmcontinue = None
            while True:
                params = {
                    "action": "query",
                    "list": "categorymembers",
                    "cmtitle": cat,
                    "cmlimit": "500",
                }
                if cmcontinue:
                    params["cmcontinue"] = cmcontinue
                try:
                    data = self._get(api_url, params)
                except Exception as e:
                    print(f"[red]Wiki category listing failed for {cat}: {e}")
                    break
                members = (data.get("query") or {}).get("categorymembers") or []
                for member in members:
                    if member.get("ns") == 14:
                        subcat = member.get("title")
                        if subcat and subcat not in seen_cats:
                            pending_cats.put(subcat)
                        continue
                    title = member.get("title")
                    if not title or title in seen_titles:
                        continue
                    seen_titles.add(title)
                    yield title
                    yielded += 1
                    if max_pages and yielded >= max_pages:
                        return
                cont = (data.get("continue") or {}).get("cmcontinue")
                if not cont:
                    break
                cmcontinue = cont

    def fetch_page(self, api_url, host, title):
        try:
            data = self._get(api_url, {
                "action": "parse",
                "page": title,
                "prop": "text",
                "redirects": "1",
            })
        except Exception as e:
            print(f"[yellow]Wiki fetch failed for '{title}': {e}")
            return None
        parse = data.get("parse") or {}
        html = ((parse.get("text") or {}).get("*")) or ""
        resolved_title = parse.get("title") or title
        text = self._html_to_text(html)
        url = f"https://{host}/wiki/{quote(resolved_title.replace(' ', '_'))}"
        return {
            "video_id": make_wiki_id(host, resolved_title),
            "title": resolved_title,
            "text": text,
            "source": "mediawiki",
            "duration_seconds": 0,
            "url": url,
            # NOTE: no revid here on purpose. `action=parse` does not return
            # one (only prop=revisions does), and adding a second per-page
            # request just to capture it would double training cost. The
            # refresh path fetches revids in cheap 50-title batches instead,
            # and backfills them onto existing entries on its first run.
        }

    def fetch_revisions(self, api_url, titles, batch_size=50):
        """Current revision id per requested title.

        Returns {requested_title: revid}. Batches titles (MediaWiki caps a
        single `titles=` param at 50) and follows redirects so a caller holding
        a canonical title still resolves. One cheap API call per 50 pages,
        which is what makes a full-corpus change check practical.
        """
        out = {}
        titles = [t for t in titles if t]
        for i in range(0, len(titles), batch_size):
            batch = titles[i:i + batch_size]
            try:
                data = self._get(api_url, {
                    "action": "query",
                    "prop": "revisions",
                    "rvprop": "ids",
                    "titles": "|".join(batch),
                    "redirects": "1",
                })
            except Exception as e:
                print(f"[yellow]Revision lookup failed for a batch of "
                      f"{len(batch)}: {e}")
                continue
            query = data.get("query") or {}
            # requested (possibly redirected/normalized) -> canonical page title
            alias = {}
            for group in ("normalized", "redirects"):
                for row in query.get(group) or []:
                    frm, to = row.get("from"), row.get("to")
                    if frm and to:
                        alias[frm] = to
            pages = query.get("pages") or {}
            for page in pages.values():
                revs = page.get("revisions") or []
                title = page.get("title")
                if title and revs and revs[0].get("revid"):
                    out[title] = revs[0]["revid"]
            # map what the caller asked for onto the canonical revid
            for requested in batch:
                if requested in out:
                    out.setdefault(requested, out[requested])
                    continue
                cur, hops = requested, 0
                while cur in alias and hops < 5:
                    cur = alias[cur]
                    hops += 1
                if cur in out:
                    out[requested] = out[cur]
            # One polite pause between batch calls; these are cheap metadata
            # lookups, not full page fetches, so use the skip delay.
            pause = config.WIKI_SKIP_DELAY if hasattr(config, "WIKI_SKIP_DELAY") else 1
            if pause > 0:
                time.sleep(pause)
        return out

    def _html_to_text(self, html):
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            import re as _re
            text = _re.sub(r"<[^>]+>", "\n", html)
            return text
        soup = BeautifulSoup(html, "html.parser")
        for tag_name in STRIP_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()
        for class_name in ("mw-editsection", "navbox", "toc", "mw-references-wrap", "printfooter"):
            for tag in soup.find_all(class_=class_name):
                tag.decompose()
        return soup.get_text(separator="\n")

    def _sleep_with_countdown(self, seconds, started, time_budget):
        end = time.time() + seconds
        try:
            while True:
                remain = end - time.time()
                if remain <= 0:
                    break
                if time_budget:
                    left = max(0, int(time_budget - (time.time() - started)))
                    mm, ss = divmod(left, 60)
                    sys.stdout.write(f"\r[Wiki] next page in {int(remain) + 1}s | session time left {mm}:{ss:02d}   ")
                else:
                    sys.stdout.write(f"\r[Wiki] next page in {int(remain) + 1}s   ")
                sys.stdout.flush()
                time.sleep(min(1, max(0.05, remain)))
        finally:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def resolve_titles(self, api_url, titles):
        """Resolve redirects + normalization for many titles in a few batch
        API calls. Returns {raw_title: resolved_title}. A redirect alias that
        resolves to a page already in the KB is therefore caught by the
        known pre-check before any expensive page fetch."""
        if not titles:
            return {}
        resolved = {}
        for i in range(0, len(titles), 50):
            chunk = titles[i:i + 50]
            try:
                data = self._get(api_url, {
                    "action": "query",
                    "titles": "|".join(chunk),
                    "redirects": "1",
                })
            except Exception as e:
                print(f"[red]Wiki redirect resolution failed: {e}")
                for t in chunk:
                    resolved[t] = t
                continue
            query = data.get("query") or {}
            norm = {n.get("from"): n.get("to") for n in query.get("normalized") or []}
            redir = {r.get("from"): r.get("to") for r in query.get("redirects") or []}
            for raw in chunk:
                cur = norm.get(raw, raw)
                seen = set()
                while cur in redir and cur not in seen:
                    seen.add(cur)
                    cur = norm.get(redir[cur], redir[cur])
                resolved[raw] = cur
        return resolved

    def train_site(self, site_url, store_fn, max_pages=None, page_delay=None, min_words=25,
                   categories=None, time_budget=None, is_known_fn=None):
        if page_delay is None:
            page_delay = config.WIKI_PAGE_DELAY
        if time_budget is None:
            time_budget = config.WIKI_TIME_BUDGET or None
        api, host = self.discover_api(site_url)
        if not api:
            return 0, 0
        if categories:
            print(f"[cyan]Enumerating pages in {len(categories)} root category(ies): {', '.join(categories)}")
            titles = list(self.list_category_titles(api, categories, max_pages=max_pages))
        else:
            print("[cyan]Enumerating all wiki pages (this may take a moment)...")
            titles = list(self.list_titles(api, max_pages=max_pages))
        print(f"[cyan]Resolving redirects for {len(titles)} pages...")
        resolved_map = self.resolve_titles(api, titles) if titles else {}
        res_aliases = sum(1 for t in titles if resolved_map.get(t, t) != t)
        if res_aliases:
            print(f"[dim]{res_aliases} redirect alias(es) will map to their canonical page")
        processed = 0
        stored_count = 0
        failed_count = 0
        skipped_known = 0
        skipped_prev = 0
        started = time.time()
        budget_hit = False
        try:
            for title in titles:
                if time_budget and (time.time() - started) >= time_budget:
                    budget_hit = True
                    break
                resolved_title = resolved_map.get(title, title)
                page_id = make_wiki_id(host, title)
                resolved_id = make_wiki_id(host, resolved_title)
                if page_id in self._skipped_by_host.get(host, set()) or \
                        resolved_id in self._skipped_by_host.get(host, set()):
                    skipped_prev += 1
                    continue
                if is_known_fn and (is_known_fn(page_id) or is_known_fn(resolved_id)):
                    skipped_known += 1
                    continue
                result = self.fetch_page(api, host, title)
                processed += 1
                skipped_page = not (result and result["text"] and len(result["text"].split()) >= min_words)
                if result is None:
                    failed_count += 1
                elif skipped_page:
                    self._skipped_by_host.setdefault(host, set()).add(page_id)
                    if resolved_id != page_id:
                        self._skipped_by_host[host].add(resolved_id)
                    print(f"[dim]Wiki: skipped '{title}' (too little readable text) — added to skip list")
                elif store_fn(result, tags=["wiki"]):
                    stored_count += 1
                if processed % 10 == 0:
                    self._save_skipped()
                    elapsed = int(time.time() - started)
                    left = max(0, time_budget - (time.time() - started)) if time_budget else None
                    left_str = f" | {int(left)//60}m{int(left)%60:02d}s session left" if left is not None else ""
                    print(f"[cyan]Wiki progress: {processed} pages fetched, {stored_count} new trained "
                          f"({skipped_known} already known, {skipped_prev} previously-skipped, "
                          f"{failed_count} failed) | {elapsed//60}m{elapsed%60:02d}s elapsed{left_str}")
                skip_delay = max(2, config.WIKI_SKIP_DELAY)
                delay_here = min(page_delay, skip_delay) if (skipped_page and page_delay > 0) else page_delay
                if delay_here > 0:
                    self._sleep_with_countdown(delay_here, started, time_budget)
        except KeyboardInterrupt:
            self._save_skipped()
            print(f"[yellow]\nWiki training interrupted — {processed} fetched, {skipped_known} known skipped, "
                  f"{skipped_prev} previously-skipped passed over, {failed_count} failed, "
                  f"{stored_count} trained. Progress kept.")
            return processed, stored_count
        finally:
            self.close_browser()
        self._save_skipped()
        if budget_hit:
            minutes = int(time_budget // 60)
            print(f"[yellow]\nWiki session budget reached ({minutes} min) — {processed} pages fetched this session, "
                  f"{stored_count} new trained, {skipped_known} already-known skipped, {skipped_prev} previously-skipped passed over.")
            print("[cyan]Run !trainwiki again to resume where it left off.")
            return processed, stored_count
        print(f"[green]Wiki training complete: {processed} pages processed, {stored_count} new pages trained "
              f"({skipped_known} already-known skipped, {skipped_prev} previously-skipped passed over, "
              f"{failed_count} failed)")
        return processed, stored_count
