r"""Regenerate the pre-exported Pocket TTS voice state.

Pocket TTS can rebuild the voice state from the reference WAV on startup,
but that costs seconds (1-12s depending on cache state). Exporting it once to a
safetensors file makes loading ~2ms instead.

Run this after changing data/speaker_reference.wav, or after switching
POCKET_LANGUAGE, then restart the bot. It must run inside the Pocket venv,
because only that venv has the pocket_tts package:

    pocket-tts-venv\Scripts\python.exe tools\export_pocket_voice.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "data", "speaker_reference.wav")
DST = os.path.join(ROOT, "data", "pocket_voice.safetensors")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import config  # noqa: E402
from rich import print  # noqa: E402


def main():
    if not os.path.exists(REF):
        print(f"[red]Reference WAV not found: {REF}")
        return 1
    language = getattr(config, "POCKET_LANGUAGE", "english_2026-09")
    steps = int(getattr(config, "POCKET_DECODE_STEPS", 1))
    print(f"[cyan]Language: {language}   decode_steps: {steps}")
    print(f"[cyan]Reference: {REF}")

    from pocket_tts import TTSModel, export_model_state

    t0 = time.perf_counter()
    model = TTSModel.load_model(language=language, sampler_decode_steps=steps)
    model.eval()
    print(f"[cyan]Model loaded in {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    state = model.get_state_for_audio_prompt(REF)
    print(f"[green]Voice state built from WAV in {time.perf_counter() - t0:.2f}s")

    export_model_state(state, DST)
    print(f"[green]Exported -> {DST} ({os.path.getsize(DST) / 1048576:.1f} MB)")

    t0 = time.perf_counter()
    reloaded = model.get_state_for_audio_prompt(DST)
    audio = model.generate_audio(reloaded, "Systems online. Ready when you are.",
                                 max_tokens=50)
    data = audio.detach().cpu().numpy().reshape(-1)
    rms = float((data ** 2).mean() ** 0.5)
    dur = len(data) / model.sample_rate
    print(f"[green]Reload {time.perf_counter() - t0:.2f}s -> {dur:.2f}s audio, rms={rms:.3f}")
    if rms < 0.005:
        print("[red]Reload produced near-silence; investigate before trusting this export.")
        return 1
    print("[green]Done. Restart the bot to pick it up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
