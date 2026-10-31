try:
    import audioop
except ModuleNotFoundError:  # removed from the stdlib in Python 3.13
    # audioop-lts provides the same top-level "audioop" name, so on 3.13+ the
    # import above succeeds once the backport is installed
    # (pip install audioop-lts). This branch only fires when it is not.
    raise ModuleNotFoundError(
        "audioop was removed from Python 3.13. Either run Python 3.12 or "
        "below, or install the backport: pip install audioop-lts"
    )
import json
import queue
import re
import threading
import time

from rich import print

import config

import keyboard
import sounddevice as sd
from vosk import Model, KaldiRecognizer

SAMPLE_RATE = 16000
CHANNELS = 1


def match_wake(text, wake_words):
    norm = " " + re.sub(r"[^a-z0-9 ]", " ", text.lower()) + " "
    for phrase in sorted(wake_words, key=len, reverse=True):
        p = f" {phrase} "
        idx = norm.find(p)
        if idx != -1:
            stripped = (norm[:idx] + " " + norm[idx + len(p):]).strip()
            return phrase, re.sub(r"\s+", " ", stripped).strip()
    return None, ""


class MicInput:
    def __init__(self, model_path, device=None):
        self.device = device
        print(f"[yellow]Loading Vosk model from: {model_path}")
        try:
            self.model = Model(model_path)
        except Exception as e:
            raise RuntimeError(
                f"Could not load Vosk model at '{model_path}'. Download one from "
                f"https://alphacephei.com/vosk/models and extract it there. "
                f"Microphone features are disabled until then. Error: {e}")
        print("[green]Vosk model loaded.")

    def _resolve_device(self):
        if self.device is not None and str(self.device) != "":
            return self._match_device(self.device)

        default = sd.default.device[0]
        if default != -1:
            try:
                if sd.query_devices(default)["max_input_channels"] > 0:
                    return default
            except Exception:
                pass

        devices = sd.query_devices()
        skip_words = ("voicemod", "virtual", "sonar", "dummy", "stereo mix", "line in")
        candidates = [
            idx for idx, d in enumerate(devices)
            if d["max_input_channels"] > 0
            and not any(word in d["name"].lower() for word in skip_words)
        ]
        if not candidates:
            raise RuntimeError("No usable microphone found! Check Windows Sound settings.")
        chosen = candidates[0]
        print(f"[yellow]No default microphone set, using: {devices[chosen]['name']} (device {chosen})")
        print("[yellow]To pick a different mic, set MIC_DEVICE in .env to its index or part of its name.")
        return chosen

    def _match_device(self, spec):
        spec_str = str(spec).strip()
        try:
            return int(spec_str)
        except ValueError:
            pass
        needle = spec_str.lower()
        for idx, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and needle in d["name"].lower():
                print(f"[green]Using microphone: {d['name']} (device {idx})")
                return idx
        raise RuntimeError(f"Microphone matching '{spec_str}' not found!")

    def _open_stream(self, device, audio_queue):
        info = sd.query_devices(device)
        native_rate = int(info["default_samplerate"]) or SAMPLE_RATE
        max_ch = max(1, int(info["max_input_channels"]))

        def callback(indata, frames, time_info, status):
            audio_queue.put(bytes(indata))

        configs = [
            (SAMPLE_RATE, 1),
            (native_rate, 1),
            (native_rate, min(2, max_ch)),
        ]
        last_err = None
        for rate, channels in configs:
            try:
                stream = sd.RawInputStream(
                    samplerate=rate,
                    blocksize=8000,
                    dtype="int16",
                    channels=channels,
                    callback=callback,
                    device=device,
                )
                if (rate, channels) != (SAMPLE_RATE, CHANNELS):
                    print(f"[yellow]Mic opened at {rate} Hz / {channels} ch (resampling to {SAMPLE_RATE} Hz)")
                return stream, rate, channels
            except sd.PortAudioError as e:
                last_err = e
        raise last_err

    def listen_continuous(self, stop_key="p"):
        audio_queue = queue.Queue()
        device = self._resolve_device()
        stream, rate, channels = self._open_stream(device, audio_queue)
        recognizer = KaldiRecognizer(self.model, rate)
        conv_state = None
        all_results = []

        with stream:
            print("[green]Listening... press P to stop")
            while True:
                if keyboard.is_pressed(stop_key):
                    while keyboard.is_pressed(stop_key):
                        time.sleep(0.05)
                    print("\n[yellow]Stopped listening.")
                    break
                try:
                    data = audio_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if rate != SAMPLE_RATE or channels != CHANNELS:
                    data, conv_state = audioop.ratecv(
                        data, 2, channels, rate, SAMPLE_RATE, conv_state
                    )
                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "").strip()
                    if text:
                        all_results.append(text)
                        print(f"[cyan]  >> {text}")

        final = recognizer.FinalResult()
        final_text = final.get("text", "").strip() if isinstance(final, dict) else ""
        if final_text:
            all_results.append(final_text)

        return " ".join(all_results).strip()


class PassiveListener:
    """Always-on wake-word ear. Passive until a wake word is heard, then
    active for WAKE_ACTIVE_WINDOW seconds, dispatching speech to on_text."""

    def __init__(self, mic, wake_words, active_window=8, on_text=None, busy_fn=None):
        self.mic = mic
        self.wake_words = [w.lower() for w in wake_words if w.strip()]
        self.active_window = active_window
        self.on_text = on_text
        self.busy_fn = busy_fn
        self._thread = None
        self._active_until = 0.0
        self._pause_evt = threading.Event()
        self._stop_evt = threading.Event()
        self._reset_req = False
        self._suppressing = False
        self._state_lock = threading.Lock()
        self._state = "stopped"

    @property
    def state(self):
        with self._state_lock:
            return self._state

    def _set_state(self, value):
        with self._state_lock:
            self._state = value

    def _is_current_thread(self):
        return self._thread is threading.current_thread()

    def start(self):
        if not self.wake_words:
            print("[yellow]Passive listening disabled: no wake words configured")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop_evt.clear()
        self._pause_evt.clear()
        self._reset_req = True
        self._set_state("starting")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def pause(self):
        """Suspend processing; the stream keeps draining so nothing backs up."""
        if self._thread and self._thread.is_alive():
            was_running = self.state == "running" or self.state == "starting"
            self._active_until = 0.0
            self._pause_evt.set()
            self._set_state("paused")
            return was_running
        return False

    def resume(self):
        """Un-pause with a clean recognizer (stale half-heard audio dropped)."""
        if self._thread and self._thread.is_alive():
            self._reset_req = True
            self._pause_evt.clear()
            self._set_state("running")
            return True
        return False

    def stop(self):
        self._stop_evt.set()
        self._pause_evt.clear()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        if not self._is_current_thread():
            self._set_state("stopped")

    def restart(self):
        self.stop()
        return self.start()

    def status_line(self):
        words = ", ".join(self.wake_words) if self.wake_words else "(none)"
        return f"Ears are {self.state}. Wake words: {words}"

    @property
    def is_listening(self):
        """True while the active conversation window is open."""
        return self.state == "running" and time.time() < self._active_until

    def _match_wake(self, text):
        return match_wake(text, self.wake_words)

    def _dispatch(self, text):
        if not text or (self.busy_fn and self.busy_fn()):
            return
        try:
            self.on_text(text)
        except Exception as e:
            print(f"[red]Passive listener dispatch error: {e}")

    def _run(self):
        audio_queue = queue.Queue()
        try:
            device = self.mic._resolve_device()
            stream, rate, channels = self.mic._open_stream(device, audio_queue)
        except Exception as e:
            print(f"[red]Passive listener could not open mic: {e}")
            if self._is_current_thread():
                self._set_state("stopped")
            return
        recognizer = KaldiRecognizer(self.model_ref(), rate)
        conv_state = None
        self._set_state("running")
        print(f"[green]Passive listening active — say {' / '.join(self.wake_words)} to talk to the bot")
        with stream:
            while not self._stop_evt.is_set():
                if self._pause_evt.is_set():
                    # Drain the queue so live audio doesn't pile up, process nothing.
                    while not self._stop_evt.is_set() and self._pause_evt.is_set():
                        try:
                            audio_queue.get(timeout=0.1)
                        except queue.Empty:
                            pass
                    continue
                if self._reset_req:
                    recognizer = KaldiRecognizer(self.model_ref(), rate)
                    conv_state = None
                    self._reset_req = False
                try:
                    data = audio_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if rate != SAMPLE_RATE or channels != CHANNELS:
                    data, conv_state = audioop.ratecv(
                        data, 2, channels, rate, SAMPLE_RATE, conv_state
                    )
                got_final = recognizer.AcceptWaveform(data)

                # While the bot is speaking, ignore everything it "hears" —
                # his own TTS bleed must never arm or feed the active window.
                # AcceptWaveform above has already ingested this audio, so on the
                # first suppressed chunk we also drop the tainted decoder state;
                # otherwise Vosk emits it later as a stale final.
                if self.busy_fn and self.busy_fn():
                    self._active_until = 0.0
                    if not self._suppressing:
                        self._reset_req = True
                        self._suppressing = True
                    continue
                self._suppressing = False

                now = time.time()
                active = now < self._active_until

                partial = ""
                if not got_final:
                    try:
                        partial = json.loads(recognizer.PartialResult()).get("partial", "")
                    except Exception:
                        partial = ""

                if got_final:
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "").strip()
                    if not text:
                        continue
                    wake, rest = self._match_wake(text)
                    if wake and rest:
                        print(f"[cyan]  (wake '{wake}') >> {rest}")
                        self._dispatch(rest)
                        self._active_until = time.time() + self.active_window
                    elif wake:
                        print(f"[green]Wake word heard ('{wake}') — listening...")
                        self._active_until = time.time() + self.active_window
                    elif active:
                        print(f"[cyan]  >> {text}")
                        self._dispatch(text)
                        self._active_until = time.time() + min(self.active_window, 4) + 2
                elif partial and active:
                    self._active_until = time.time() + self.active_window
                elif partial and not active and config.WAKE_PARTIAL_ARM:
                    wake, _ = self._match_wake(partial)
                    if wake:
                        self._active_until = time.time() + max(self.active_window,
                                                               int((len(partial.split()) + 6) * 0.7))
        if self._is_current_thread():
            self._set_state("stopped")
        print("[yellow]Passive listening stopped.")

    def model_ref(self):
        return self.mic.model
