import os
import queue
import threading
import time

import sounddevice as sd
import soundfile as sf
from rich import print


class AudioManager:
    def __init__(self, device=None, busy_grace_ms=400):
        self.device = device
        self._busy = threading.Event()
        self._busy_grace = max(0.0, busy_grace_ms / 1000.0)
        self._busy_until = 0.0
        self._play_queue = queue.Queue()
        self._worker = threading.Thread(target=self._play_worker, daemon=True)
        self._worker.start()

    @property
    def is_busy(self):
        """True only while samples are actively playing. Use for visuals."""
        return self._busy.is_set()

    @property
    def is_audible(self):
        """True while playing OR the speaker tail is still ringing out.

        Use this to gate microphone input: the gap between queue chunks and
        the decay after the final chunk are both short windows where the bot
        would otherwise hear himself.
        """
        return self._busy.is_set() or time.monotonic() < self._busy_until

    def _resolve_device(self):
        if self.device is None or str(self.device).strip() == "":
            return None
        spec_str = str(self.device).strip()
        try:
            return int(spec_str)
        except ValueError:
            pass
        needle = spec_str.lower()
        for idx, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and needle in d["name"].lower():
                print(f"[green]Using speakers: {d['name']} (device {idx})")
                return idx
        print(f"[yellow]Speaker '{spec_str}' not found, using system default")
        return None

    def _read_audio(self, file_path):
        try:
            data, samplerate = sf.read(file_path, dtype="float32")
            return data, samplerate
        except Exception as e:
            print(f"[red]Could not read audio file {file_path}: {e}")
            return None, None

    def _play_worker(self):
        while True:
            file_path, delete_file = self._play_queue.get()
            if file_path is None:
                break
            try:
                data, samplerate = self._read_audio(file_path)
                if data is not None:
                    device = self._resolve_device()
                    self._busy.set()
                    sd.play(data, samplerate, device=device)
                    sd.wait()
            finally:
                self._busy.clear()
                # Re-arm the mic gate for the speaker decay tail, and cover the
                # gap before the next queued chunk starts playing.
                self._busy_until = time.monotonic() + self._busy_grace
                if delete_file and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except PermissionError:
                        print(f"[yellow]Could not delete {file_path}")
            self._play_queue.task_done()

    def enqueue_audio(self, file_path, delete_file=False):
        """Queue audio for playback and return immediately.

        Use this when more chunks are still being synthesized: the worker keeps
        playing the previous chunk while the next one renders, instead of the
        caller waiting for each one to finish before starting the next.
        """
        self._play_queue.put((file_path, delete_file))

    def wait_until_idle(self):
        """Block until everything currently queued has finished playing."""
        self._play_queue.join()

    def play_audio(self, file_path, delete_file=False):
        self.enqueue_audio(file_path, delete_file)
        self.wait_until_idle()

    def stop(self):
        sd.stop()
