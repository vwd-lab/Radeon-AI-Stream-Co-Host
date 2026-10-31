from abc import ABC, abstractmethod


class StreamTool(ABC):
    @abstractmethod
    def on_ai_speaking(self):
        pass

    @abstractmethod
    def on_ai_done(self):
        pass

    @abstractmethod
    def set_enabled(self, enabled):
        pass
