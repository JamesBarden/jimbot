#!/usr/bin/env python3
"""
Backfill ledger CSVs for historical sessions.

Going forward, `main.py` saves the ledger automatically at session
shutdown.  This script is for older sessions whose game URL was never
recorded — you supply (session_stamp, game_url) pairs explicitly.

Two modes:

  1. Single pair:
        python3 scripts/fetch_ledgers.py 2026-04-25_092157 \
            https://www.pokernow.com/games/pglVPLcg0zqz2KeCAz3SKkS5J

  2. Batch from a TSV/CSV mapping file (lines: "<stamp>\\t<url>"):
        python3 scripts/fetch_ledgers.py --map ledger_backfill.tsv

Either mode writes Logs/ledger_<stamp>.csv.  Existing files are
overwritten — re-running is safe.
"""
from __future__ import annotations
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from ledger_fetch import fetch_ledger  # noqa: E402

LOG_DIR = os.path.join(ROOT, "Logs")


def _backfill_one(stamp: str, url: str) -> bool:
    dest = os.path.join(LOG_DIR, f"ledger_{stamp}.csv")
    print(f"[backfill] {stamp} ← {url}")
    ok = fetch_ledger(url, dest)
    print(f"           {'saved' if ok else 'FAILED'} → {dest}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stamp", nargs="?", help="session_id stamp, e.g. 2026-04-25_092157")
    ap.add_argument("url",   nargs="?", help="pokernow game URL")
    ap.add_argument("--map", help="path to a TSV file with '<stamp>\\t<url>' per line")
    args = ap.parse_args()

    if args.map:
        if not os.path.isfile(args.map):
            print(f"[backfill] map file not found: {args.map}")
            sys.exit(2)
        ok_count = 0
        total    = 0
        with open(args.map) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1) if "\t" not in line else line.split("\t", 1)
                if len(parts) != 2:
                    print(f"[backfill] skipping bad line: {line!r}")
                    continue
                total += 1
                if _backfill_one(parts[0].strip(), parts[1].strip()):
                    ok_count += 1
        print(f"[backfill] {ok_count}/{total} succeeded")
        sys.exit(0 if ok_count == total else 1)

    if not args.stamp or not args.url:
        ap.print_help()
        sys.exit(1)
    sys.exit(0 if _backfill_one(args.stamp, args.url) else 1)


if __name__ == "__main__":
    main()
