import os

MUTED = os.getenv("VOICE_MUTE", "0") == "1"


def is_voice_muted():
    return MUTED


def set_voice_muted(muted):
    global MUTED
    MUTED = bool(muted)
    return MUTED