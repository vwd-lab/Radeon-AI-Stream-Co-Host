import os
from rich import print


class LocalFileExtractor:
    def __init__(self, whisper_model="base"):
        self.whisper_model = whisper_model

    def extract(self, file_path):
        if not os.path.exists(file_path):
            print(f"[red]File not found: {file_path}")
            return None

        print(f"[yellow]Extracting from local file: {file_path}")

        try:
            import whisper
            model = whisper.load_model(self.whisper_model)
            result = model.transcribe(file_path)
            full_text = result["text"]
            title = os.path.splitext(os.path.basename(file_path))[0]

            return {
                "video_id": title.lower().replace(" ", "_"),
                "title": title,
                "text": full_text,
                "source": "local_file",
                "duration_seconds": 0,
            }
        except Exception as e:
            print(f"[red]Local file extraction failed: {e}")
            return None

    def extract_directory(self, directory_path):
        if not os.path.isdir(directory_path):
            print(f"[red]Directory not found: {directory_path}")
            return []

        extensions = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".mp3", ".wav", ".m4a", ".flac", ".ogg"}
        results = []
        for filename in sorted(os.listdir(directory_path)):
            ext = os.path.splitext(filename)[1].lower()
            if ext in extensions:
                file_path = os.path.join(directory_path, filename)
                result = self.extract(file_path)
                if result:
                    results.append(result)
        return results
