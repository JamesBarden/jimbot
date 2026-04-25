"""
Ledger fetch — pulls the authoritative session P&L CSV from pokernow.

Pokernow exposes the per-game ledger as a public CSV at:
    https://www.pokernow.com/games/{id}/ledger_{id}.csv

Schema:
    player_nickname, player_id, session_start_at, session_end_at,
    buy_in, buy_out, stack, net          (all chip values, integer)

This module is the single source of truth for "what actually happened
this session" — every other P&L number in the bot's logs is an
approximation that can drift when the bot misses hand boundaries.

Usage as a library (called from main.py shutdown):
    from ledger_fetch import fetch_ledger
    fetch_ledger(game_url, "/path/to/Logs/ledger_<stamp>.csv")

Usage as a script (manual / backfill):
    python3 ledger_fetch.py <game_url> <dest_path>
"""
from __future__ import annotations
import os
import re
import sys
import urllib.request
from urllib.error import URLError, HTTPError

GAME_ID_RE = re.compile(r"/games/([A-Za-z0-9_\-]+)")
TIMEOUT_SECS = 10

# Pokernow sits behind Cloudflare; the default urllib User-Agent
# ("Python-urllib/3.x") gets a 403.  A plain browser UA goes through.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def extract_game_id(game_url: str) -> str | None:
    m = GAME_ID_RE.search(game_url)
    return m.group(1) if m else None


def ledger_url(game_url: str) -> str | None:
    gid = extract_game_id(game_url)
    if not gid:
        return None
    return f"https://www.pokernow.com/games/{gid}/ledger_{gid}.csv"


def fetch_ledger(game_url: str, dest_path: str) -> bool:
    """Download the ledger CSV. Returns True on success, False on any failure."""
    url = ledger_url(game_url)
    if not url:
        print(f"[ledger] WARN: could not extract game id from url={game_url!r}")
        return False
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            data = resp.read()
    except (HTTPError, URLError, OSError) as e:
        print(f"[ledger] WARN: fetch failed: {e}")
        return False
    if not data:
        print("[ledger] WARN: fetch returned empty body")
        return False
    try:
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
    except OSError as e:
        print(f"[ledger] WARN: write failed: {e}")
        return False
    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 ledger_fetch.py <game_url> <dest_path>")
        sys.exit(1)
    ok = fetch_ledger(sys.argv[1], sys.argv[2])
    if ok:
        print(f"[ledger] saved → {sys.argv[2]}")
    sys.exit(0 if ok else 1)
