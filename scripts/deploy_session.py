#!/usr/bin/env python3
"""
Deploy the latest session to the dashboard.

Rebuilds docs/data/metrics.json from the latest Logs/ and commits + pushes
to origin so the GitHub Pages dashboard updates.

Does NOT bump the version — the version should have been bumped whenever
the code changed (see scripts/bump_version.py and CLAUDE.md).

Usage:
  python3 scripts/deploy_session.py                       # rebuild + commit + push
  python3 scripts/deploy_session.py --no-push             # commit only
  python3 scripts/deploy_session.py --message "add ..."   # custom message suffix
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _version() -> str:
    try:
        with open(os.path.join(ROOT, "VERSION")) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "0.0"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=ROOT, check=check, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="commit but don't push")
    ap.add_argument("--message", default="", help="suffix appended to commit message")
    args = ap.parse_args()

    # 1. Sanity: we're in a git repo with an origin
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            cwd=ROOT, capture_output=True, text=True)
    if inside.returncode != 0:
        print("[deploy] not a git repo — skipping commit/push")
        return
    remotes = subprocess.run(["git", "remote"], cwd=ROOT, capture_output=True, text=True)
    has_origin = "origin" in remotes.stdout.split()

    # 2. Rebuild data
    print("[deploy] rebuilding dashboard data ...")
    _run([sys.executable, os.path.join(HERE, "build_dashboard_data.py")])

    # 3. Stage ONLY the metrics file — code changes (including VERSION bumps) belong
    #    to separate code-change commits with proper semver messages.
    _run(["git", "add", "docs/data/metrics.json"])

    # 4. Anything to commit?
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if diff.returncode == 0:
        print("[deploy] metrics.json unchanged — nothing to commit")
        return

    # 5. Commit
    version = _version()
    default_msg = f"Jimbot v{version}: session data refresh"
    msg = f"{default_msg}{' — ' + args.message if args.message else ''}"
    _run(["git", "commit", "-m", msg])

    # 6. Push
    if args.no_push:
        print("[deploy] --no-push: leaving commit local. Push with: git push")
        return
    if not has_origin:
        print("[deploy] no 'origin' remote configured — commit is local only")
        return
    _run(["git", "push"])
    print(f"[deploy] dashboard updated for Jimbot v{version}")


if __name__ == "__main__":
    main()
