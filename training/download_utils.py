import subprocess
import threading
import time

from rich import print


def run_with_heartbeat(cmd, timeout_seconds, label):
    """Run a subprocess printing a heartbeat every 30s so long downloads
    don't look hung. Hard-kills at timeout via watchdog thread (works even
    when the child produces no output). Returns (returncode, output_tail).

    Shared by the Twitch VOD and YouTube audio downloaders so long media
    fetches behave identically: visible progress, configurable ceiling
    (VOD_DOWNLOAD_TIMEOUT), clean hard-kill instead of a hung shell.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    start = time.time()
    last_beat = start
    output_tail = []
    timed_out = threading.Event()

    def _watchdog():
        if proc.poll() is None:
            timed_out.set()
            proc.kill()

    guard = threading.Timer(timeout_seconds, _watchdog)
    guard.daemon = True
    guard.start()
    try:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            output_tail.append(text)
            if len(output_tail) > 50:
                del output_tail[:25]
            now = time.time()
            if now - last_beat >= 30:
                elapsed = int(now - start)
                print(f"[dim]...{label} still running ({elapsed // 60}m{elapsed % 60:02d}s elapsed)")
                last_beat = now
        proc.wait(timeout=30)
    finally:
        guard.cancel()
        if proc.poll() is None:
            proc.kill()
    if timed_out.is_set():
        raise TimeoutError(
            f"{label} exceeded {timeout_seconds // 60} minute limit "
            f"(VOD_DOWNLOAD_TIMEOUT)"
        )
    return proc.returncode, "".join(output_tail)


def fetch_media_title(url, timeout_seconds=120):
    """Quick yt-dlp title lookup, kept separate from the download itself."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--print", "title", url],
            capture_output=True, text=True, timeout=timeout_seconds,
        )
        if result.stdout and result.stdout.strip():
            return result.stdout.strip().split("\n")[0]
    except Exception as e:
        print(f"[yellow]Title lookup failed: {e}")
    return None
