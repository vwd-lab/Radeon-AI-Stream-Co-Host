import json
import os
from datetime import datetime
from rich import print


MAX_RECENT_MESSAGES = 10


class ChatterMemory:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = {"chatters": {}, "global_stats": {"total_interactions": 0, "unique_chatters": 0}}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                print(f"[green]Loaded chatter memory: {len(self.data.get('chatters', {}))} chatters")
            except (json.JSONDecodeError, IOError):
                print("[yellow]Could not load chatter memory, starting fresh")

    def save(self):
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get_profile(self, username):
        key = username.lower()
        if key not in self.data["chatters"]:
            self.data["chatters"][key] = {
                "display_name": username,
                "first_seen": datetime.now().isoformat(),
                "last_seen": datetime.now().isoformat(),
                "interaction_count": 0,
                "recent_messages": [],
            }
        return self.data["chatters"][key]

    def add_message(self, username, role, content):
        profile = self.get_profile(username)
        profile["recent_messages"].append({
            "timestamp": datetime.now().isoformat(),
            "role": role,
            "content": content,
        })
        if len(profile["recent_messages"]) > MAX_RECENT_MESSAGES:
            profile["recent_messages"] = profile["recent_messages"][-MAX_RECENT_MESSAGES:]

    def increment_interaction(self, username):
        profile = self.get_profile(username)
        profile["interaction_count"] += 1
        profile["last_seen"] = datetime.now().isoformat()
        self.data["global_stats"]["total_interactions"] += 1
        unique_keys = set(self.data["chatters"].keys())
        self.data["global_stats"]["unique_chatters"] = len(unique_keys)

    def get_context_string(self, username):
        profile = self.get_profile(username)
        count = profile["interaction_count"]
        if count == 0:
            return ""
        return (f"You know this chatter '{profile['display_name']}':\n"
                f"- Returning visitor ({count} previous interactions)")
