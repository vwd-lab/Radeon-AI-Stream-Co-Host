import json
import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from rich import print

import config


def _channel_day():
    """Calendar day key in the reset timezone (drives daily quota rollover)."""
    return datetime.now(ZoneInfo(config.RESET_TZ)).strftime('%Y-%m-%d')


class MemoryDB:
    def __init__(self, filepath):
        self.filepath = filepath
        folder = os.path.dirname(os.path.abspath(filepath))
        os.makedirs(folder, exist_ok=True)
        self.conn = sqlite3.connect(filepath, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self._create_table()

    def _create_table(self):
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                fact TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used TEXT,
                use_count INTEGER DEFAULT 0,
                UNIQUE(username COLLATE NOCASE, fact COLLATE NOCASE)
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS token_usage (
                day TEXT PRIMARY KEY,
                total_tokens INTEGER DEFAULT 0
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS request_usage (
                day TEXT PRIMARY KEY,
                requests INTEGER DEFAULT 0
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS channel_usage (
                day TEXT NOT NULL,
                channel TEXT NOT NULL,
                requests INTEGER DEFAULT 0,
                PRIMARY KEY (day, channel)
            )
        ''')
        self.conn.commit()

    def add_token_usage(self, amount):
        if not amount or amount <= 0:
            return self.get_tokens_used_today()
        today = datetime.now().strftime('%Y-%m-%d')
        self.conn.execute('''
            INSERT INTO token_usage (day, total_tokens) VALUES (?, ?)
            ON CONFLICT(day) DO UPDATE SET total_tokens = total_tokens + excluded.total_tokens
        ''', (today, int(amount)))
        self.conn.commit()
        return self.get_tokens_used_today()

    def get_tokens_used_today(self):
        today = datetime.now().strftime('%Y-%m-%d')
        row = self.conn.execute(
            'SELECT total_tokens FROM token_usage WHERE day = ?', (today,)
        ).fetchone()
        return row[0] if row else 0

    def add_request_usage(self, amount=1):
        today = datetime.now().strftime('%Y-%m-%d')
        self.conn.execute('''
            INSERT INTO request_usage (day, requests) VALUES (?, ?)
            ON CONFLICT(day) DO UPDATE SET requests = requests + excluded.requests
        ''', (today, int(amount)))
        self.conn.commit()
        return self.get_requests_today()

    def get_requests_today(self):
        today = datetime.now().strftime('%Y-%m-%d')
        row = self.conn.execute(
            'SELECT requests FROM request_usage WHERE day = ?', (today,)
        ).fetchone()
        return row[0] if row else 0

    def add_channel_usage(self, channel, amount=1):
        today = _channel_day()
        self.conn.execute('''
            INSERT INTO channel_usage (day, channel, requests) VALUES (?, ?, ?)
            ON CONFLICT(day, channel) DO UPDATE SET requests = requests + excluded.requests
        ''', (today, channel.strip(), int(amount)))
        self.conn.commit()
        return self.get_channel_usage(channel)

    def get_channel_usage(self, channel):
        today = _channel_day()
        row = self.conn.execute(
            'SELECT requests FROM channel_usage WHERE day = ? AND channel = ?',
            (today, channel.strip()),
        ).fetchone()
        return row[0] if row else 0

    def add_memory(self, username, fact):
        fact = re.sub(r"\s+", " ", fact.strip().rstrip(".!?,:;")).strip()
        if not fact or len(fact) > 300:
            return False
        try:
            self.conn.execute(
                'INSERT INTO memories (username, fact, created_at) VALUES (?, ?, ?)',
                (username.strip(), fact, datetime.now().isoformat(timespec='seconds')),
            )
            self.conn.commit()
            print(f'[green]Memory saved for {username}: {fact}')
            return True
        except sqlite3.IntegrityError:
            return False

    def get_memories(self, username, limit=8):
        rows = self.conn.execute(
            'SELECT fact FROM memories WHERE username = ? COLLATE NOCASE ORDER BY id DESC LIMIT ?',
            (username.strip(), int(limit)),
        ).fetchall()
        return [row[0] for row in rows]

    def forget(self, username, fact):
        cur = self.conn.execute(
            'DELETE FROM memories WHERE username = ? COLLATE NOCASE AND fact = ? COLLATE NOCASE',
            (username.strip(), fact.strip()),
        )
        self.conn.commit()
        removed = cur.rowcount > 0
        if removed:
            print(f'[green]Removed memory for {username}: {fact}')
        return removed

    def list_for(self, username):
        rows = self.conn.execute(
            'SELECT fact, created_at FROM memories WHERE username = ? COLLATE NOCASE ORDER BY id',
            (username.strip(),),
        ).fetchall()
        return rows

    STREAK_PREFIX = "Watch streak: "

    def get_streak(self, username):
        """Return the highest recorded watch-streak count for a viewer (0 if none)."""
        best = 0
        for fact in self.get_memories(username, limit=100):
            if fact.startswith(self.STREAK_PREFIX):
                try:
                    best = max(best, int(re.sub(r"\D", "", fact[len(self.STREAK_PREFIX):])))
                except ValueError:
                    continue
        return best

    def set_streak(self, username, count):
        """Record a viewer's watch-streak count (replaces any prior streak fact)."""
        username = username.strip()
        for fact in list(self.get_memories(username, limit=100)):
            if fact.startswith(self.STREAK_PREFIX):
                self.forget(username, fact)
        self.add_memory(username, f"{self.STREAK_PREFIX}{int(count)} streams")

    def has_raided(self, username):
        """True if this streamer has raided the bot before (persisted across restarts)."""
        return any(
            fact.startswith("Raided: ")
            for fact in self.get_memories(username.strip(), limit=100)
        )

    def mark_raided(self, username):
        """Remember that this streamer has raided, so a repeat raid stays 'friendly'."""
        username = username.strip()
        if not self.has_raided(username):
            self.add_memory(username, "Raided: Yes (has brought a raiding party before)")

    def context_block(self, username, limit=8):
        facts = self.get_memories(username, limit=limit)
        if not facts:
            return ''
        lines = [f"Long-term memories about '{username}' (use naturally if relevant):"]
        lines.extend(f'- {fact}' for fact in facts)
        return '\n'.join(lines)

    def context_block_multi(self, usernames, per_user=5):
        blocks = []
        seen = set()
        for username in usernames:
            key = username.strip()
            if not key or key.lower() in seen:
                continue
            seen.add(key.lower())
            facts = self.get_memories(key, limit=int(per_user))
            if not facts:
                continue
            lines = [f"Long-term memories about '{key}':"]
            lines.extend(f'- {fact}' for fact in facts)
            blocks.append('\n'.join(lines))
        if not blocks:
            return ''
        header = "Long-term memories (each section belongs to that specific chatter only):"
        return header + '\n\n' + '\n\n'.join(blocks)

    def import_from_chatter_json(self, path):
        if not os.path.exists(path):
            return 0
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return 0
        imported = 0
        for key, profile in data.get('chatters', {}).items():
            name = profile.get('display_name', key)
            for fact in profile.get('facts', []):
                if self.add_memory(name, fact):
                    imported += 1
        return imported

    def close(self):
        self.conn.close()
