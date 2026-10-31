import os
import re
import tempfile

from rich import print

from training.download_utils import run_with_heartbeat as _run_with_heartbeat, fetch_media_title


def extract_vod_id(url):
    patterns = [
        r'twitch\.tv\/videos\/(\d+)',
        r'twitch\.tv\/\w+\/v\/(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


class TwitchVodExtractor:
    def __init__(self, whisper_model="base"):
        self.whisper_model = whisper_model

    def extract(self, url):
        vod_id = extract_vod_id(url)
        if not vod_id:
            print(f"[red]Could not extract VOD ID from: {url}")
            return None

        print(f"[yellow]Extracting Twitch VOD: {vod_id}")

        temp_dir = tempfile.mkdtemp()
        audio_path = os.path.join(temp_dir, "vod_audio.mp3")

        try:
            print("[yellow]Downloading VOD audio with yt-dlp...")
            title = fetch_media_title(url) or f"Twitch VOD {vod_id}"

            import config
            download_timeout = config.VOD_DOWNLOAD_TIMEOUT
            returncode, output = _run_with_heartbeat(
                ["yt-dlp", "-f", "bestaudio", "-N", "4",
                 "-x", "--audio-format", "mp3",
                 "-o", audio_path, url],
                download_timeout, "VOD audio download",
            )

            actual_audio = audio_path
            if not os.path.exists(audio_path):
                for f in os.listdir(temp_dir):
                    if f.endswith((".mp3", ".wav", ".m4a", ".opus")):
                        actual_audio = os.path.join(temp_dir, f)
                        break

            if not os.path.exists(actual_audio):
                print(f"[red]Failed to download VOD audio (exit {returncode}): {output[-500:]}")
                return None

            print("[yellow]Transcribing with Whisper...")
            import whisper
            model = whisper.load_model(self.whisper_model)
            result = model.transcribe(actual_audio)
            full_text = result["text"]

            for f in os.listdir(temp_dir):
                os.remove(os.path.join(temp_dir, f))
            os.rmdir(temp_dir)

            return {
                "video_id": vod_id,
                "title": title,
                "text": full_text,
                "source": "twitch_vod",
                "duration_seconds": 0,
            }
        except Exception as e:
            print(f"[red]Twitch VOD extraction failed: {e}")
            return None
