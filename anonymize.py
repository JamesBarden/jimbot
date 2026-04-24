"""
Stable opponent-name anonymization.

A single local salt (stored in .anon_salt, gitignored) lets us produce the
same pseudo-ID for the same opponent across sessions while keeping real names
off any pushed data. The mapping is NOT invertible without the salt.

Usage:
    from anonymize import anon
    pseudo = anon("SlowPeteFromSlack")   # → "player_a1b2"
"""
from __future__ import annotations
import hashlib
import os
import secrets

_HERE     = os.path.dirname(os.path.abspath(__file__))
_SALT_FP  = os.path.join(_HERE, ".anon_salt")

_PREFIX   = "player_"
_LEN      = 4            # short enough to read, wide enough to avoid collisions in realistic pools


def _load_or_create_salt() -> bytes:
    if os.path.isfile(_SALT_FP):
        return open(_SALT_FP, "rb").read().strip()
    # First run: create a 32-byte random salt and persist
    salt = secrets.token_bytes(32)
    with open(_SALT_FP, "wb") as f:
        f.write(salt)
    os.chmod(_SALT_FP, 0o600)
    return salt


_SALT: bytes | None = None


def anon(username: str) -> str:
    """Return a stable pseudo-ID for a username. Empty input → 'player_anon'."""
    if not username:
        return "player_anon"
    global _SALT
    if _SALT is None:
        _SALT = _load_or_create_salt()
    h = hashlib.blake2b(username.encode("utf-8"), key=_SALT, digest_size=8).hexdigest()
    return _PREFIX + h[:_LEN]


def anon_list(names) -> list:
    """Anonymize every name in an iterable. Preserves order."""
    return [anon(n) for n in names]
