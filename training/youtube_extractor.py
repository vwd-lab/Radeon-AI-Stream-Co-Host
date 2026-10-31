import re
from rich import print

from training.download_utils import run_with_heartbeat, fetch_media_title


def extract_video_id(url):
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


class YouTubeExtractor:
    def __init__(self):
        self.api = None
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            self.api = YouTubeTranscriptApi()
        except ImportError:
            print("[yellow]youtube-transcript-api not installed. YouTube extraction will use yt-dlp + Whisper.")

    def extract(self, url, language="en"):
        video_id = extract_video_id(url)
        if not video_id:
            print(f"[red]Could not extract video ID from: {url}")
            return None

        print(f"[yellow]Extracting YouTube transcript for: {video_id}")

        if self.api:
            result = self._try_captions(video_id, language)
            if result:
                return result

        print("[yellow]No captions available, falling back to yt-dlp + Whisper...")
        return self._fallback_whisper(url, video_id)

    def _try_captions(self, video_id, language):
        try:
            transcript = self.api.fetch(video_id, languages=[language])
            text_parts = []
            for snippet in transcript:
                text_parts.append(snippet.text)
            full_text = " ".join(text_parts)

            title = self._get_title(video_id)
            return {
                "video_id": video_id,
                "title": title,
                "text": full_text,
                "source": "youtube_captions",
                "duration_seconds": 0,
            }
        except Exception as e:
            print(f"[yellow]Caption extraction failed: {e}")
            return None

    def _get_title(self, video_id):
        return f"YouTube Video {video_id}"

    def _fallback_whisper(self, url, video_id):
        try:
            import tempfile
            import os

            temp_dir = tempfile.mkdtemp()
            audio_path = os.path.join(temp_dir, "audio.mp3")

            print("[yellow]Downloading audio with yt-dlp...")
            title = fetch_media_title(url) or f"YouTube Video {video_id}"

            import config
            download_timeout = config.VOD_DOWNLOAD_TIMEOUT
            returncode, output = run_with_heartbeat(
                ["yt-dlp", "-f", "bestaudio", "-N", "4",
                 "-x", "--audio-format", "mp3",
                 "-o", audio_path, url],
                download_timeout, "YouTube audio download",
            )

            actual_audio = audio_path
            if not os.path.exists(audio_path):
                for f in os.listdir(temp_dir):
                    if f.endswith((".mp3", ".wav", ".m4a", ".opus")):
                        actual_audio = os.path.join(temp_dir, f)
                        break

            if not os.path.exists(actual_audio):
                print(f"[red]Failed to download audio (exit {returncode}): {output[-500:]}")
                return None

            print("[yellow]Transcribing with Whisper...")
            import whisper
            model = whisper.load_model("base")
            result = model.transcribe(actual_audio)
            full_text = result["text"]

            for f in os.listdir(temp_dir):
                os.remove(os.path.join(temp_dir, f))
            os.rmdir(temp_dir)

            return {
                "video_id": video_id,
                "title": title,
                "text": full_text,
                "source": "youtube_whisper",
                "duration_seconds": 0,
            }
        except Exception as e:
            print(f"[red]Whisper fallback failed: {e}")
            return None
