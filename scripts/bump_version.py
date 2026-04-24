#!/usr/bin/env python3
"""
Bump the VERSION file by one level.

Usage:
  python3 scripts/bump_version.py patch   # 1.3.2 → 1.3.3  (tiny fixes — optional)
  python3 scripts/bump_version.py minor   # 1.3.2 → 1.4.0  (default: any code change)
  python3 scripts/bump_version.py major   # 1.3.2 → 2.0.0  (breaking / big overhaul)
  python3 scripts/bump_version.py         # defaults to 'minor'
  python3 scripts/bump_version.py --show  # print current version, no write

Version format: MAJOR.MINOR (.PATCH optional). The dashboard groups sessions
by MAJOR.MINOR so patch-level bumps share a comparison bucket.

Writes the new version to VERSION and prints it to stdout.
"""
from __future__ import annotations
import os
import sys

HERE    = os.path.dirname(os.path.abspath(__file__))
VFILE   = os.path.join(os.path.dirname(HERE), "VERSION")


def read_version() -> tuple[int, int, int]:
    if not os.path.isfile(VFILE):
        return (1, 0, 0)
    raw = open(VFILE).read().strip()
    parts = raw.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return (major, minor, patch)
    except (ValueError, IndexError):
        return (1, 0, 0)


def format_version(v: tuple[int, int, int]) -> str:
    major, minor, patch = v
    return f"{major}.{minor}" if patch == 0 else f"{major}.{minor}.{patch}"


def bump(current: tuple[int, int, int], level: str) -> tuple[int, int, int]:
    major, minor, patch = current
    if level == "major":
        return (major + 1, 0, 0)
    if level == "minor":
        return (major, minor + 1, 0)
    if level == "patch":
        return (major, minor, patch + 1)
    raise ValueError(f"unknown level: {level!r}")


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if args and args[0] == "--show":
        print(format_version(read_version()))
        return

    level = args[0] if args else "minor"
    if level not in ("major", "minor", "patch"):
        print(f"error: level must be major/minor/patch (got {level!r})", file=sys.stderr)
        sys.exit(1)

    current = read_version()
    nxt     = bump(current, level)
    with open(VFILE, "w") as f:
        f.write(format_version(nxt) + "\n")

    print(f"Jimbot v{format_version(current)} → v{format_version(nxt)}")


if __name__ == "__main__":
    main()
