import time
import requests
from rich import print

import config

GQL_URL = "https://gql.twitch.tv/gql"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
PERSISTED_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

VIDEO_QUERY = """
query($id: ID!) {
  video(id: $id) {
    id
    title
    createdAt
    lengthSeconds
  }
}
"""


class ChatReplayDownloader:
    def __init__(self, timeout=30):
        self.timeout = timeout

    def _gql(self, query, variables):
        resp = requests.post(
            GQL_URL,
            json={"query": query, "variables": variables},
            headers={"Client-ID": GQL_CLIENT_ID},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_video_info(self, video_id):
        try:
            data = self._gql(VIDEO_QUERY, {"id": str(video_id)})
            return data.get("data", {}).get("video")
        except Exception as e:
            print(f"[red]Chat replay: failed to fetch video info: {e}")
            return None

    def _fetch_comments_page(self, video_id, offset=None):
        variables = {"videoID": str(video_id)}
        if offset is not None:
            variables["contentOffsetSeconds"] = int(offset)
        body = [{
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": variables,
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": PERSISTED_COMMENTS_HASH,
                }
            },
        }]
        resp = requests.post(GQL_URL, json=body, headers={"Client-ID": GQL_CLIENT_ID}, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        for err in data.get("errors") or []:
            message = err.get("message", "")
            if "PersistedQueryNotFound" in message or "integrity check" in message.lower():
                raise RuntimeError(message)
        return ((data.get("data") or {}).get("video") or {}).get("comments")

    def fetch_comments(self, video_id, max_comments=15000, page_delay=0.15):
        comments = []
        offset = 0
        stale_pages = 0
        while len(comments) < max_comments:
            try:
                block = self._fetch_comments_page(video_id, offset=offset)
            except Exception as e:
                print(f"[red]Chat replay fetch interrupted after {len(comments)} messages: {e}")
                break
            if not block:
                break
            edges = block.get("edges") or []
            if not edges:
                break
            last_seconds = offset
            for edge in edges:
                node = edge.get("node") or {}
                commenter = node.get("commenter") or {}
                user = commenter.get("displayName") or commenter.get("login") or "unknown"
                if user.lower() in config.IGNORED_USERS:
                    continue
                seconds = node.get("contentOffsetSeconds")
                if seconds is None:
                    continue
                last_seconds = seconds
                fragments = ((node.get("message") or {}).get("fragments")) or []
                text = "".join(f.get("text", "") for f in fragments).strip()
                if text:
                    comments.append((seconds, user, text))
                    if len(comments) >= max_comments:
                        break
            page = (block.get("pageInfo") or {})
            if not page.get("hasNextPage"):
                break
            next_offset = last_seconds + 1
            if next_offset <= offset:
                stale_pages += 1
                if stale_pages > 2:
                    break
            else:
                stale_pages = 0
                offset = next_offset
            time.sleep(page_delay)
        return comments[:max_comments]

    @staticmethod
    def format_timestamp(seconds):
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"

    def format_transcript(self, comments, title="", created_at=""):
        lines = [f"[Twitch VOD Chat Replay] {title}"]
        if created_at:
            lines.append(f"Streamed: {created_at[:10]}")
        lines.append("Viewer reactions and conversation during this stream:")
        lines.extend(
            f"[{self.format_timestamp(offset)}] {user}: {text}"
            for offset, user, text in comments
        )
        return "\n".join(lines)

    def extract(self, vod_id, title=None, max_comments=15000):
        """Returns (result_dict_or_None, comments_list) — comments returned
        even when transcript storage is skipped, so callers can mine them."""
        info = self.get_video_info(vod_id)
        if not info:
            return None, []
        resolved_title = title or info.get("title") or f"Twitch VOD {vod_id}"
        created = info.get("createdAt") or ""
        total_length = info.get("lengthSeconds") or 0
        print(f"[yellow]Downloading chat replay ({max_comments} comment cap)...")
        comments = self.fetch_comments(vod_id, max_comments=max_comments)
        if not comments:
            print("[yellow]No chat replay available for this VOD (may be disabled or expired)")
            return None, comments
        print(f"[green]Fetched {len(comments)} chat messages")
        text = self.format_transcript(comments, resolved_title, created)
        result = {
            "video_id": f"twitch_chat_{vod_id}",
            "title": f"{resolved_title} (chat replay)",
            "text": text,
            "source": "twitch_vod_chat",
            "duration_seconds": total_length,
        }
        return result, comments
