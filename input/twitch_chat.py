import json
import re
import threading
import time
import urllib.request
import queue
from irc import client as irc_client
from rich import print

import config
from input.chat_listener import ChatListener, safe_ambient


def validate_oauth_token(token):
    """Check the token with Twitch before trying to use it.

    A revoked or truncated token is still a plausible-looking string, so IRC
    just reports "Login authentication failed" and the bot silently reads no
    chat. Validating up front turns that into a specific, actionable message.
    Returns (ok, info_or_reason).
    """
    raw = (token or "").strip().strip('"')
    if not raw:
        return False, "TWITCH_OAUTH_TOKEN is empty in .env"
    bare = raw[6:] if raw.lower().startswith("oauth:") else raw
    # Token length is NOT fixed - observed 30 and 31 char tokens in the wild -
    # so only flag something far too short to be real. Twitch's own validate
    # endpoint is the authority; this is just a fast, clear hint.
    if len(bare) < 20:
        return False, (f"token is {len(bare)} chars - far too short to be a "
                       f"real token, this looks like a partial paste")
    try:
        req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {bare}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            info = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return False, (f"token rejected by Twitch ({type(e).__name__}) - "
                       f"it is expired, revoked, or from another app")
    return True, info


class TwitchChatListener(ChatListener):
    def __init__(self, bot_name, channel, oauth_token, command_prefix="!ask", mention_name=None,
                 message_queue=None, on_ambient=None):
        super().__init__()
        self.bot_name = bot_name.lower()
        self.mention_name = (mention_name or bot_name).lower()
        self.channel = channel.lower()
        self.oauth_token = oauth_token
        self.command_prefix = command_prefix.lower()
        self.message_queue = message_queue or queue.Queue()
        self.on_ambient = on_ambient
        self.reactor = irc_client.Reactor()
        self.server = None
        self._thread = None
        # Twitch allows short bursts but throttles beyond ~1 msg / 1.5s and
        # refuses duplicates within 30s.
        self.MIN_SEND_INTERVAL = 1.5
        self._last_send = 0.0
        self._last_text = ""

    @staticmethod
    def _tag_dict(event):
        tags = getattr(event, "tags", None) or []
        return {t.get("key"): t.get("value") for t in tags if t.get("key")}

    def _on_connect(self, connection, event):
        connection.join(f"#{self.channel}")
        print(f"[green]Connected to Twitch chat: #{self.channel}")

    def _on_usernotice(self, connection, event):
        tags = self._tag_dict(event)
        msg_id = tags.get("msg-id")
        # Twitch sends USERNOTICE from the server (source.nick == "tmi.twitch.tv"),
        # not the user, so the real login only exists in the tags. Reading nick
        # here used to record raids and watch-streaks against the IRC hostname.
        username = (tags.get("login") or tags.get("display-name") or "").strip().lower()
        if not username:
            nick = (event.source.nick or "").strip().lower() if event.source else ""
            username = "" if nick == "tmi.twitch.tv" else nick
        if not username or username in config.IGNORED_USERS:
            return

        if msg_id == "raid":
            display = (tags.get("msg-param-displayName") or username or "a streamer").strip()
            try:
                party = int(tags.get("msg-param-viewerCount") or 0)
            except (TypeError, ValueError):
                party = 0
            print(f"[magenta][Twitch] RAID from {display} with {party} viewers")
            self.message_queue.put(("raid", "", username, party, display))
            return

        if msg_id == "viewermilestone" and tags.get("msg-param-category") == "watch-streak":
            try:
                count = int(tags.get("msg-param-value") or 0)
            except (TypeError, ValueError):
                count = 0
            display = (tags.get("display-name") or username or "viewer").strip()
            print(f"[magenta][Twitch] Watch streak by {username}: {count}")
            self.message_queue.put(("streak", "", username, count, display))
            return

    def _on_pubmsg(self, connection, event):
        username = event.source.nick.lower()
        message = event.arguments[0].strip()

        if username == self.bot_name:
            return

        if username in config.IGNORED_USERS:
            return

        if self.on_ambient:
            safe_ambient(self.on_ambient, username, message)

        if message.lower().startswith(self.command_prefix + " "):
            user_msg = message[len(self.command_prefix) + 1:].strip()
            if user_msg:
                formatted = f"[Twitch viewer '{username}' asks]: {user_msg}"
                print(f"[magenta][Twitch] {username}: {message}")
                self.message_queue.put(("chat", formatted, username))
                return

        if self.mention_name in message.lower():
            cleaned = re.sub(re.escape(self.mention_name), "", message, flags=re.IGNORECASE).strip()
            if cleaned:
                formatted = f"[Twitch viewer '{username}' says]: {cleaned}"
                print(f"[magenta][Twitch] {username}: {message}")
                self.message_queue.put(("chat", formatted, username))
                return

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("[green]Twitch chat listener started in background thread.")

    def _run(self):
        try:
            # Fail loudly and specifically here, rather than as an opaque IRC
            # "Login authentication failed" once the reactor is already running.
            ok, info = validate_oauth_token(self.oauth_token)
            if not ok:
                print(f"[red]Twitch chat NOT starting: {info}")
                print("[red]  Re-authorize: Twitch -> Settings -> Connections, "
                      "or re-run the implicit-flow URL for your app.")
                print("[red]  Log in as the bot account "
                      f"'{self.bot_name}' and copy the new 31-char token "
                      "into TWITCH_OAUTH_TOKEN in .env")
                self._running = False
                return
            login = info.get("login", "?")
            if login and login.lower() != self.bot_name.lower():
                print(f"[yellow]Twitch token belongs to '{login}' but "
                      f"TWITCH_BOT_NICK is '{self.bot_name}' - IRC will reject "
                      f"this. They must match.")
            else:
                print(f"[green]Twitch token valid for '{login}' "
                      f"({', '.join(info.get('scopes') or []) or 'no scopes'})")

            irc_conn = self.reactor.server()
            self.server = irc_conn
            token = self.oauth_token.strip()
            if not token.lower().startswith("oauth:"):
                token = f"oauth:{token}"
            irc_conn.connect("irc.chat.twitch.tv", 6667, self.bot_name, password=token)
            irc_conn.cap("REQ", "twitch.tv/tags", "twitch.tv/commands")
            irc_conn.add_global_handler("welcome", self._on_connect)
            irc_conn.add_global_handler("pubmsg", self._on_pubmsg)
            irc_conn.add_global_handler("usernotice", self._on_usernotice)
            self.reactor.process_forever()
        except Exception as e:
            print(f"[red]Twitch chat connection error: {e}")

    def stop(self):
        self._running = False
        if self.server:
            self.server.disconnect()

    def send(self, text):
        """Post a reply to the channel. Returns True if it went out.

        One Twitch account can be shared by several bots, so the body is
        prefixed with TWITCH_REPLY_PREFIX to say which one is speaking.

        Twitch rejects duplicate messages inside a 30s window and throttles
        bursts, so identical consecutive replies are skipped rather than sent
        and retried.
        """
        clean = " ".join((text or "").split()).strip()
        if not clean:
            return False
        name = (getattr(config, "TWITCH_REPLY_PREFIX", "") or "").strip()
        body = f"{name} - {clean}" if name else clean
        if not self.server:
            print("[red]Twitch send skipped: not connected yet")
            return False
        if body == self._last_text and (time.time() - self._last_send) < 30:
            print("[yellow]Twitch send skipped: identical message within 30s")
            return False
        gap = time.time() - self._last_send
        if gap < self.MIN_SEND_INTERVAL:
            time.sleep(self.MIN_SEND_INTERVAL - gap)
        try:
            self.server.privmsg(f"#{self.channel}", body[:500])
            self._last_send = time.time()
            self._last_text = body
            label = name or "Bot"
            print(f"[bold green]{label}:[/bold green] {clean}")
            return True
        except Exception as e:
            print(f"[red]Twitch send failed: {type(e).__name__}: {str(e)[:120]}")
            return False

