#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.49"]
# ///
"""Sign in to YouTube Music once and hand the session to Vector and Cortana.

    ./deploy/pi/ytmusic-login.py               # opens a small Chrome window; sign in there
    ./deploy/pi/ytmusic-login.py --no-upload   # only check that the sign-in works

Run it on the laptop. It opens a dedicated Chrome profile (kept in
~/.local/share/vector-ytmusic, used for nothing else, so normal browsing never rotates
these cookies), waits until YouTube Music is signed in, reads the session cookies and
pipes them over SSH into each agent's container, which encrypts them with its own
YTMUSIC_KEY before anything is written (cookies.enc in its data folder). Nothing is
written to this repository, to the Pi's disk in the clear, or to the laptop outside that
profile. Run it again whenever the assistants say their YouTube sign-in has expired; if
the profile is still signed in it finishes by itself.

Google refuses sign-ins in a browser it can tell is being driven ("This browser or app
may not be secure"), and a debugging port alone is enough to trip that. So there are two
launches of the same profile:

1. a plain Chrome with no debugging or automation switches, for the sign-in. The script
   only watches the profile's cookie database on disk to see when the session appears;
2. after that window is closed, a hidden Chrome with a debugging port that visits no
   page, from which Playwright reads the cookies.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

MUSIC = "https://music.youtube.com/"
SIGN_IN = "https://accounts.google.com/ServiceLogin?service=youtube&continue=" + MUSIC
PROFILE = Path(os.environ.get("YTMUSIC_PROFILE", Path.home() / ".local/share/vector-ytmusic/chrome-profile"))
PI_HOST = os.environ.get("PI_HOST", "john@192.168.1.39")
PI_KEY = os.environ.get("PI_KEY", str(Path.home() / ".ssh/rpi_key"))
CONTAINERS = ("vector-agent", "cortana-agent")
# run inside each container: keep the cookie text on stdin, encrypted, and check it works
STORE = "from app import config; from agent_core import ytmusic; ytmusic.cli()"
NEEDED = {"__Secure-3PAPISID", "__Secure-3PSID", "LOGIN_INFO"}
KEEP_DOMAINS = ("youtube.com", "google.com")
# Same cookie encryption in both launches, so the hidden one can read what the visible one
# stored (a hidden Chrome does not reliably reach the desktop wallet).
COMMON = [f"--user-data-dir={PROFILE}", "--password-store=basic", "--no-first-run", "--no-default-browser-check"]


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def find_browser() -> str:
    for name in (os.environ.get("YTMUSIC_BROWSER"), "google-chrome-stable", "google-chrome", "chromium"):
        if name and shutil.which(name):
            return shutil.which(name)
    sys.exit("No Chrome or Chromium found; set YTMUSIC_BROWSER to the browser's path.")


def quit_browser(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()                           # a normal quit, so the profile keeps the session
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def names_on_disk() -> set[str]:
    """Cookie names Chrome has saved for YouTube. Names are stored in the clear; values are not read."""
    for db in (PROFILE / "Default/Cookies", PROFILE / "Default/Network/Cookies"):
        if not db.exists():
            continue
        with tempfile.TemporaryDirectory() as tmp:     # copy: Chrome holds the live file open
            copy = Path(tmp) / "Cookies"
            try:
                shutil.copy2(db, copy)
                for ext in ("-wal", "-journal"):
                    side = db.with_name(db.name + ext)
                    if side.exists():
                        shutil.copy2(side, copy.with_name(copy.name + ext))
                con = sqlite3.connect(copy)
                try:
                    rows = con.execute("select name from cookies where host_key like '%youtube.com'").fetchall()
                finally:
                    con.close()
                return {r[0] for r in rows}
            except (OSError, sqlite3.Error):
                return set()
    return set()


def signed_in_on_disk() -> bool:
    return NEEDED <= names_on_disk()


def sign_in(timeout_s: int) -> None:
    """Launch 1: an ordinary Chrome window, nothing attached. Returns once the session is on disk."""
    PROFILE.mkdir(parents=True, exist_ok=True)
    PROFILE.chmod(0o700)
    if signed_in_on_disk():
        say("This profile is already signed in.")
        return
    proc = subprocess.Popen([find_browser(), *COMMON, "--window-size=560,820", f"--app={SIGN_IN}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    say("Sign in to your Google account in the window that opened. It closes by itself afterwards.")
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                if signed_in_on_disk():
                    return
                sys.exit("The window was closed before YouTube Music was signed in.")
            if signed_in_on_disk():
                time.sleep(6)                      # let the music page finish loading and save
                return
            time.sleep(2)
        sys.exit("Timed out waiting for the sign-in.")
    finally:
        quit_browser(proc)


def read_cookies() -> list[dict]:
    """Launch 2: the same profile, hidden, visiting nothing; read its cookies over the debugging port."""
    port_file = PROFILE / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    proc = subprocess.Popen([find_browser(), *COMMON, "--headless=new", "--remote-debugging-port=0", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while not (port_file.exists() and port_file.read_text().split()):
            if proc.poll() is not None or time.monotonic() > deadline:
                sys.exit("The hidden browser did not start.")
            time.sleep(0.2)
        port = int(port_file.read_text().split()[0])
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            try:
                return browser.contexts[0].cookies()
            finally:
                browser.close()                    # for a CDP attachment this only disconnects
    finally:
        quit_browser(proc)


def netscape(cookies: list[dict]) -> tuple[str, int]:
    lines = ["# Netscape HTTP Cookie File", "# Written by deploy/pi/ytmusic-login.py", ""]
    count = 0
    for c in cookies:
        domain = c["domain"]
        if not domain.lstrip(".").endswith(KEEP_DOMAINS):
            continue
        expires = int(c["expires"]) if c.get("expires") and c["expires"] > 0 else 0
        lines.append("\t".join([
            ("#HttpOnly_" if c.get("httpOnly") else "") + domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path") or "/",
            "TRUE" if c.get("secure") else "FALSE",
            str(expires), c["name"], c["value"]]))
        count += 1
    return "\n".join(lines) + "\n", count


def upload(text: str) -> bool:
    """Hand the cookies to each agent on the Pi. They travel over SSH into the container's
    stdin; the agent encrypts them and reports whether the sign-in works."""
    ok = True
    for name in CONTAINERS:
        remote = f"sudo -n docker exec -i {name} python -c {shlex.quote(STORE)} store"
        done = subprocess.run(["ssh", "-i", PI_KEY, "-o", "IdentitiesOnly=yes", PI_HOST, remote],
                              input=text.encode(), capture_output=True)
        out = (done.stdout.decode() + done.stderr.decode()).strip().splitlines()
        say(out[-1] if out else f"{name}: no answer")
        ok = ok and done.returncode == 0
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for the sign-in (default 600)")
    ap.add_argument("--no-upload", action="store_true", help="only check that the sign-in works")
    args = ap.parse_args()

    sign_in(args.timeout)
    cookies = read_cookies()
    missing = NEEDED - {c["name"] for c in cookies}
    if missing:
        sys.exit("Signed in, but the hidden browser could not read the session (missing "
                 + ", ".join(sorted(missing)) + ").")
    text, count = netscape(cookies)
    if args.no_upload:
        say(f"Signed in; {count} cookies read, nothing uploaded.")
        return
    say(f"Signed in; handing {count} cookies to the agents on {PI_HOST}.")
    if not upload(text):
        sys.exit("At least one agent did not take the sign-in (see above).")


if __name__ == "__main__":
    main()
