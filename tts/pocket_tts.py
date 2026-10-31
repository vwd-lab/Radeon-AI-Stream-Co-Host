import io
import time
import urllib.request
import uuid


from tts.base_tts import TextToSpeech
from rich import print  # noqa: E402

class PocketTTS(TextToSpeech):
    """Pocket TTS via a local HTTP server (see start_pocket_tts.bat).

    Default engine: it clones the reference voice and runs entirely on CPU at
    roughly RTF 0.25, which is about 4x faster than real time. Keeping it on
    CPU leaves VRAM free for the game and any local LLM.

    This is an HTTP client rather than an in-process model on purpose. Pocket
    needs a recent CPU torch build, while the rest of the stack may be running
    under a GPU torch build whose CPU kernels are much slower. Measured on the
    development machine, the same weights ran at RTF 0.25 under a CPU torch
    build and RTF 2.6 under a GPU build. So the model lives in its own venv and
    this class just calls it.
    """

    def __init__(self, server_url="", timeout=60):
        self.server_url = (server_url or "http://localhost:8123").rstrip("/")
        self.timeout = timeout
        self.device = "cpu"
        # Engines are swapped via !tts, which treats a falsy .model as "load
        # failed" and silently keeps the old engine. There is no in-process
        # model here (it lives in the server), so expose a truthy sentinel
        # while the server is reachable and None when it is not.
        self.model = None

        if not self._healthy():
            print(f"[red]Pocket TTS server not reachable at {self.server_url}.")
            print("[yellow]Start it with start_pocket_tts.bat (start_bot.bat "
                  "does this for you). On this branch Pocket is the only "
                  "engine, so the bot will start with no voice.")
            self.available = False
            return

        self.available = True
        self.model = self  # truthy sentinel; see note above
        print(f"[green]Pocket TTS server ready ({self.server_url}, cpu)")

    def _healthy(self):
        try:
            with urllib.request.urlopen(f"{self.server_url}/health", timeout=10) as r:
                return b"healthy" in r.read().lower()
        except Exception:
            return False

    def synthesize(self, text, output_path):
        if not getattr(self, "available", False):
            print("[red]Pocket TTS server unavailable!")
            return None
        if not text or not text.strip():
            return None
        try:
            boundary = f"------cohost{uuid.uuid4().hex}".encode()
            body = (b"--" + boundary +
                    b"\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n" +
                    text.encode("utf-8") + b"\r\n--" + boundary + b"--\r\n")
            req = urllib.request.Request(
                f"{self.server_url}/tts", data=body,
                headers={"Content-Type":
                         f"multipart/form-data; boundary={boundary.decode()}"})
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
            elapsed = time.perf_counter() - t0

            # The server streams WAV chunks and never patches the RIFF/data
            # sizes, so the header claims a ~11-hour file. Rewriting through
            # soundfile both fixes the header and is how audio/player.py reads
            # audio, so the saved file has to be a real WAV by the time it
            # reaches the player.
            import numpy as np
            import soundfile as sf

            audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
            audio = np.asarray(audio).reshape(-1)
            if not len(audio):
                print("[red]Pocket TTS returned empty audio.")
                return None
            sf.write(output_path, audio, sr)
            dur = len(audio) / float(sr)
            print(f"[dim]Pocket TTS {elapsed:.2f}s for {dur:.2f}s audio "
                  f"(RTF {elapsed / max(dur, 0.01):.2f})")
            return output_path
        except Exception as e:
            print(f"[red]Pocket TTS error: {e}")
            return None
