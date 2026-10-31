import hashlib
import re
from urllib.parse import urlparse

from rich import print

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

STRIP_TAGS = [
    "script", "style", "noscript", "nav", "header", "footer",
    "aside", "iframe", "form", "svg", "button", "select", "template",
]

MAX_HTML_BYTES = 2 * 1024 * 1024


def make_page_id(url):
    parsed = urlparse(url)
    normalized = f"{parsed.netloc.lower()}{parsed.path.rstrip('/').lower()}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"web_{digest}"


class WebsiteExtractor:
    def __init__(self, user_agent=DEFAULT_USER_AGENT, timeout=20):
        self.user_agent = user_agent
        self.timeout = timeout
        self._requests = None
        self._bs4 = None

    def extract(self, url):
        print(f"[yellow]Fetching website: {url}")

        response = self._fetch(url)
        if not response:
            return None

        content_type = response.headers.get("Content-Type", "")
        if content_type and not content_type.lower().startswith("text/html"):
            print(f"[red]Unsupported content type: {content_type}")
            return None

        html = response.text[:MAX_HTML_BYTES]
        title, text = self._parse_html(html, url)
        if not text or len(text.split()) < 20:
            print("[red]Not enough readable text found on page")
            return None

        return {
            "video_id": make_page_id(url),
            "title": title,
            "text": text,
            "source": "website",
            "duration_seconds": 0,
            "url": url,
        }

    def _fetch(self, url):
        try:
            import requests
            self._requests = requests
        except ImportError:
            print("[red]requests not installed. Run: pip install requests")
            return None

        try:
            response = self._requests.get(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response
        except Exception as e:
            print(f"[red]Failed to fetch {url}: {e}")
            return None

    def _parse_html(self, html, url):
        soup = self._make_soup(html)
        if soup is None:
            title = self._title_from_regex(html) or urlparse(url).netloc
            text = self._strip_with_regex(html)
            return title, self._clean_text(text)

        for tag_name in STRIP_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        title = None
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
        if not title and soup.title and soup.title.string:
            title = soup.title.string.strip()
        if not title:
            title = urlparse(url).netloc

        root = soup.find("article") or soup.find("main") or soup.body or soup
        text = root.get_text(separator="\n")
        return title, self._clean_text(text)

    def _make_soup(self, html):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, "html.parser")
        except ImportError:
            print("[yellow]beautifulsoup4 not installed. Using basic HTML stripping.")
            print("[yellow]For better extraction run: pip install beautifulsoup4")
            return None

    def _title_from_regex(self, html):
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
        return None

    def _strip_with_regex(self, html):
        text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", "\n", text)
        return text

    def _clean_text(self, text):
        lines = []
        seen_blank = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                if seen_blank:
                    continue
                seen_blank = True
            else:
                seen_blank = False
            lines.append(line)
        cleaned = "\n".join(lines).strip()
        return re.sub(r"[\u00a0\u200b]", " ", cleaned)
