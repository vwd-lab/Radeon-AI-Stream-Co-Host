"""Radeon AI Co-Host setup GUI — tkinter (stdlib only, no extra pip install).

Lets you set the bot's identity (BOT_NAME / STREAMER_NAME and the related
streamer / Twitch fields) and write them to .env without editing the file by
hand. Values are read from .env on startup, so you can re-run this at any time
to change them, then restart start_bot.bat for the change to take effect.

Works whether you are plugging into Radeon AI Co-Host or using it as a
template for your own bot.
"""
import os
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(ROOT, ".env")
DEFAULT_ENV_PATH = os.path.join(ROOT, ".env.example")

# Identity fields in the order they should appear in the GUI.
# Each entry: (env key, label, hint, quote value)
FIELDS = [
    ("BOT_NAME", "Bot name", 'What the co-host calls itself (spoken + chat prefix)', True),
    ("STREAMER_NAME", "Streamer name", 'How the bot refers to you (no spaces, no titles)', True),
    ("STREAMER_ALIASES", "Extra owner accounts", 'Optional. Other logins that get owner commands, e.g. an alt or kick:yourkickname', False),
    ("STREAMER_TWITCH_NAME", "Twitch login", "Your Twitch account login (no @, no spaces)", True),
    ("TWITCH_BOT_NICK", "Bot Twitch nick", "Login of the bot account (the OAuth token owner)", True),
    ("TWITCH_CHANNEL", "Twitch channel", "Channel to join, without the leading #", True),
]


def load_env():
    """Return dict of KEY=value from .env, preserving nothing else."""
    data = {}
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    # Strip surrounding quotes if present.
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]
                    data[k] = v
        except OSError:
            pass
    return data


def save_env(values):
    """Write values back to .env, preserving comments and unknown keys.

    Matching keys are replaced in place; keys not already present are appended.
    """
    lines = []
    seen = set()
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

    out = []
    for line in lines:
        stripped = line.strip()
        replaced = False
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in values:
                val = values[k]
                out.append(f'{k}="{val}"')
                seen.add(k)
                replaced = True
        if not replaced:
            out.append(line)

    # Append any keys that were not already in the file.
    for k, v in values.items():
        if k not in seen:
            out.append(f'{k}="{v}"')

    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        return True
    except OSError as e:
        messagebox.showerror("Save failed", f"Could not write {ENV_PATH}:\n{e}")
        return False


class SetupGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Radeon AI Co-Host — Setup")
        self.root.resizable(False, False)

        current = load_env()

        frm = ttk.Frame(root, padding=16)
        frm.grid(row=0, column=0, sticky="nsew")

        ttk.Label(
            frm,
            text="Set the bot identity, then click Save. Restart start_bot.bat "
                 "for the change to take effect.",
            wraplength=420,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self.vars = {}
        for i, (key, label, hint, quote) in enumerate(FIELDS, start=1):
            ttk.Label(frm, text=label, font=("Segoe UI", 10, "bold")).grid(
                row=i, column=0, sticky="w", pady=(6, 0))
            var = tk.StringVar(value=current.get(key, ""))
            self.vars[key] = var
            ent = ttk.Entry(frm, textvariable=var, width=52)
            ent.grid(row=i + 1, column=0, columnspan=2, sticky="we", pady=(0, 2))
            ttk.Label(frm, text=hint, foreground="#666666").grid(
                row=i + 2, column=0, columnspan=2, sticky="w", pady=(0, 8))

        # Buttons
        btns = ttk.Frame(frm)
        btns.grid(row=len(FIELDS) * 3 + 1, column=0, columnspan=2, sticky="e", pady=(12, 0))

        ttk.Button(btns, text="Cancel", command=root.destroy).grid(
            row=0, column=0, padx=(0, 8))
        ttk.Button(btns, text="Save to .env", command=self.save).grid(
            row=0, column=1, padx=(0, 8))
        ttk.Button(btns, text="Save & start bot", command=self.save_and_start).grid(
            row=0, column=2)

        root.bind("<Return>", lambda e: self.save())

    def save(self):
        values = {k: v.get().strip() for k, v in self.vars.items()}

        # Basic validation: streamer name must not contain spaces (it is matched
        # against chat usernames which carry no spaces).
        sn = values.get("STREAMER_NAME", "")
        if sn and (" " in sn or "\t" in sn):
            messagebox.showwarning(
                "Invalid streamer name",
                "STREAMER_NAME must not contain spaces or titles. "
                "Use STREAMER_ALIASES for alternate spellings instead.")
            return

        if not values.get("BOT_NAME"):
            values["BOT_NAME"] = "Bot Name"
        if not values.get("STREAMER_NAME"):
            values["STREAMER_NAME"] = "Streamer"

        if save_env(values):
            messagebox.showinfo(
                "Saved",
                "Wrote identity settings to .env.\n\n"
                "Restart start_bot.bat "
                "for the change to take effect.")
            self.root.destroy()

    def save_and_start(self):
        values = {k: v.get().strip() for k, v in self.vars.items()}
        sn = values.get("STREAMER_NAME", "")
        if sn and (" " in sn or "\t" in sn):
            messagebox.showwarning(
                "Invalid streamer name",
                "STREAMER_NAME must not contain spaces or titles. "
                "Use STREAMER_ALIASES for alternate spellings instead.")
            return
        if not save_env(values):
            return
        # Launch the bot in a new window, detached from this GUI.
        try:
            startupflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(
                ["cmd", "/c", "start_bot.bat"],
                cwd=ROOT,
                creationflags=startupflags,
                close_fds=True,
            )
        except Exception as e:
            messagebox.showwarning(
                "Launch skipped",
                f"Settings saved, but could not auto-start start_bot.bat:\n{e}")
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except Exception:
        pass
    SetupGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
