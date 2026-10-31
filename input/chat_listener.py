from abc import ABC, abstractmethod

from rich import print


def safe_ambient(on_ambient, user, message):
    try:
        on_ambient(user, message)
    except Exception as e:
        print(f"[red]Ambient hook error: {e}")


class ChatListener(ABC):
    def __init__(self):
        self._running = False

    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def stop(self):
        pass
