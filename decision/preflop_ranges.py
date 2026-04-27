"""
GTO-inspired preflop range matrices for 6-max 100BB cash games.

Each table maps hand_key -> (raise_freq, call_freq, fold_freq).

  RFI scenario      : raise=open, call=0 (no limping), fold=fold
  VS_RAISE scenario : raise=3bet, call=flat-call, fold=fold

Hands not listed default to pure fold.
Fractional frequencies implement mixed strategies — the engine samples
randomly so the same hand plays differently each session.
"""

from browser.state import Card

_RANKS = "23456789TJQKA"


def hand_key(hole_cards: list) -> str:
    """Return canonical hand string: 'AKs', 'AKo', 'AA', '76s', etc."""
    c1, c2 = hole_cards
    r1, r2 = _RANKS.index(c1.rank), _RANKS.index(c2.rank)
    if r1 < r2:
        c1, c2 = c2, c1
    if c1.rank == c2.rank:
        return c1.rank * 2
    return c1.rank + c2.rank + ("s" if c1.suit == c2.suit else "o")


def _rfi(d: dict) -> dict:
    """Convert {hand: raise_freq} → {hand: (raise_freq, 0.0, fold_freq)}."""
    return {h: (f, 0.0, round(1.0 - f, 4)) for h, f in d.items()}


def _vs(d: dict) -> dict:
    """Convert {hand: (3bet_freq, call_freq)} → full (r, c, f) tuples."""
    return {h: (r, c, round(max(0.0, 1.0 - r - c), 4)) for h, (r, c) in d.items()}


# ── RFI: raise-first-in open frequencies ─────────────────────────────────────
# Unlisted hands = fold. No limping (call_freq always 0 for opens).

_UTG = {
    "AA": 1.0, "KK": 1.0, "QQ": 1.0, "JJ": 1.0, "TT": 1.0, "99": 1.0,
    "88": 0.7, "77": 0.4, "66": 0.2,
    "AKs": 1.0, "AQs": 1.0, "AJs": 1.0, "ATs": 1.0, "A9s": 0.6, "A5s": 0.6,
    "AKo": 1.0, "AQo": 1.0, "AJo": 0.7, "ATo": 0.4,
    "KQs": 1.0, "KJs": 1.0, "KTs": 0.8,
    "KQo": 1.0, "KJo": 0.4,
    "QJs": 1.0, "QTs": 0.7,
    "JTs": 1.0,
    "T9s": 0.3, "98s": 0.2,
}

_HJ = {
    **_UTG,
    "88": 1.0, "77": 0.8, "66": 0.5, "55": 0.3,
    "A9s": 1.0, "A8s": 0.7, "A5s": 1.0, "A4s": 0.5,
    "AJo": 1.0, "ATo": 0.8,
    "KTs": 1.0, "K9s": 0.7,
    "KJo": 0.7, "KTo": 0.3,
    "QTs": 1.0, "Q9s": 0.5,
    "QJo": 0.5,
    "J9s": 0.6,
    "T9s": 0.7, "98s": 0.5, "87s": 0.3,
}

_CO = {
    **_HJ,
    "66": 1.0, "55": 0.8, "44": 0.5, "33": 0.3,
    "A8s": 1.0, "A7s": 0.8, "A6s": 0.6, "A4s": 0.8, "A3s": 0.5, "A2s": 0.3,
    "A9o": 0.6, "A8o": 0.3,
    "K9s": 1.0, "K8s": 0.7,
    "KTo": 0.7, "K9o": 0.3,
    "Q8s": 0.5,
    "QTo": 0.6, "Q9o": 0.3,
    "J9s": 1.0, "J8s": 0.6,
    "JTo": 0.7, "J9o": 0.3,
    "T8s": 0.7,
    "T9o": 0.3,
    "98s": 1.0, "97s": 0.5,
    "87s": 0.7, "86s": 0.3,
    "76s": 0.7, "65s": 0.5,
    "QJo": 0.9,
}

_BTN = {
    **_CO,
    "44": 0.9, "33": 0.7, "22": 0.6,
    "A7s": 1.0, "A6s": 1.0, "A5s": 1.0, "A4s": 1.0, "A3s": 1.0, "A2s": 1.0,
    "A9o": 0.9, "A8o": 0.6, "A7o": 0.5, "A6o": 0.4, "A5o": 0.7,
    "A4o": 0.5, "A3o": 0.3, "A2o": 0.2,
    "K8s": 1.0, "K7s": 0.8, "K6s": 0.6, "K5s": 0.4, "K4s": 0.3, "K3s": 0.2,
    "K9o": 0.6, "K8o": 0.3, "K7o": 0.2,
    "Q8s": 0.9, "Q7s": 0.5, "Q6s": 0.3,
    "Q9o": 0.5, "Q8o": 0.2,
    "J8s": 1.0, "J7s": 0.5,
    "J9o": 0.4, "J8o": 0.2,
    "T8s": 1.0, "T7s": 0.5,
    "T9o": 0.5, "T8o": 0.2,
    "97s": 0.9, "96s": 0.4,
    "98o": 0.3,
    "86s": 0.7, "85s": 0.3,
    "87o": 0.2,
    "76s": 1.0, "75s": 0.5,
    "65s": 0.9, "64s": 0.3,
    "54s": 0.8, "53s": 0.3,
    "43s": 0.4,
    "QJo": 1.0, "QTo": 0.8,
    "JTo": 0.9,
}

_SB = {
    **_BTN,
    "22": 0.8, "33": 0.8, "44": 1.0,
    "A2s": 1.0, "A3s": 1.0,
}

_RFI = {
    "UTG":     _rfi(_UTG),
    "HJ":      _rfi(_HJ),
    "CO":      _rfi(_CO),
    "BTN":     _rfi(_BTN),
    "SB":      _rfi(_SB),
    "BB":      {},            # BB never opens
    "MP":      _rfi(_HJ),     # treat MP as HJ
    "unknown": _rfi(_CO),     # default to CO if position unknown
}


# ── VS_RAISE: facing a single open raise ─────────────────────────────────────
# (3bet_freq, call_freq) — fold_freq = 1 - 3bet - call
# SB has tighter calls (OOP postflop); BB has wider calls (discount + closing action).

_UTG_VR = {
    "AA": (1.0, 0.0), "KK": (1.0, 0.0), "QQ": (0.8, 0.2),
    "JJ": (0.4, 0.6), "TT": (0.1, 0.8), "99": (0.0, 0.7), "88": (0.0, 0.5),
    "AKs": (1.0, 0.0), "AQs": (0.8, 0.2), "AJs": (0.2, 0.6),
    "A5s": (0.5, 0.0), "A4s": (0.3, 0.0),
    "AKo": (1.0, 0.0), "AQo": (0.5, 0.4),
    "KQs": (0.3, 0.6),
    "QJs": (0.0, 0.5), "JTs": (0.0, 0.5), "T9s": (0.0, 0.4),
}

_HJ_VR = {
    "AA": (1.0, 0.0), "KK": (1.0, 0.0), "QQ": (1.0, 0.0), "JJ": (0.7, 0.3),
    "TT": (0.3, 0.7), "99": (0.0, 0.8), "88": (0.0, 0.6), "77": (0.0, 0.4),
    "66": (0.0, 0.3), "55": (0.0, 0.2), "44": (0.0, 0.1),
    "AKs": (1.0, 0.0), "AQs": (0.9, 0.1), "AJs": (0.5, 0.5), "ATs": (0.2, 0.6),
    "A5s": (0.6, 0.0), "A4s": (0.4, 0.0), "A3s": (0.2, 0.0),
    "AKo": (1.0, 0.0), "AQo": (0.7, 0.3), "AJo": (0.1, 0.5),
    "KQs": (0.5, 0.5), "KJs": (0.1, 0.7),
    "KQo": (0.2, 0.5),
    "QJs": (0.0, 0.7), "JTs": (0.0, 0.6), "T9s": (0.0, 0.5),
    "98s": (0.0, 0.4), "87s": (0.0, 0.3),
}

_CO_VR = {
    "AA": (1.0, 0.0), "KK": (1.0, 0.0), "QQ": (1.0, 0.0), "JJ": (0.6, 0.4),
    "TT": (0.2, 0.8), "99": (0.0, 0.9), "88": (0.0, 0.8), "77": (0.0, 0.7),
    "66": (0.0, 0.5), "55": (0.0, 0.4), "44": (0.0, 0.3), "33": (0.0, 0.2), "22": (0.0, 0.1),
    "AKs": (1.0, 0.0), "AQs": (0.8, 0.2), "AJs": (0.4, 0.6), "ATs": (0.2, 0.8),
    "A9s": (0.0, 0.7), "A8s": (0.0, 0.6),
    "A5s": (0.6, 0.0), "A4s": (0.4, 0.0), "A3s": (0.3, 0.0), "A2s": (0.2, 0.0),
    "AKo": (1.0, 0.0), "AQo": (0.6, 0.4), "AJo": (0.1, 0.7), "ATo": (0.0, 0.5),
    "KQs": (0.5, 0.5), "KJs": (0.1, 0.8), "KTs": (0.0, 0.7),
    "KQo": (0.2, 0.6), "KJo": (0.0, 0.5),
    "QJs": (0.0, 0.8), "QTs": (0.0, 0.7),
    "JTs": (0.0, 0.8), "J9s": (0.0, 0.5),
    "T9s": (0.0, 0.6), "98s": (0.0, 0.5), "87s": (0.0, 0.4), "76s": (0.0, 0.3),
}

_BTN_VR = {
    "AA": (1.0, 0.0), "KK": (1.0, 0.0), "QQ": (0.9, 0.1), "JJ": (0.5, 0.5),
    "TT": (0.2, 0.8), "99": (0.0, 1.0), "88": (0.0, 0.9), "77": (0.0, 0.8),
    "66": (0.0, 0.5), "55": (0.0, 0.4), "44": (0.0, 0.3), "33": (0.0, 0.2), "22": (0.0, 0.2),
    "AKs": (1.0, 0.0), "AQs": (0.7, 0.3), "AJs": (0.3, 0.7), "ATs": (0.2, 0.8),
    "A9s": (0.0, 0.8), "A8s": (0.0, 0.7), "A7s": (0.0, 0.6), "A6s": (0.0, 0.5),
    "A5s": (0.5, 0.0), "A4s": (0.4, 0.0), "A3s": (0.3, 0.0), "A2s": (0.2, 0.0),
    "AKo": (1.0, 0.0), "AQo": (0.5, 0.5), "AJo": (0.1, 0.8), "ATo": (0.0, 0.7),
    "KQs": (0.4, 0.6), "KJs": (0.1, 0.9), "KTs": (0.0, 0.9), "K9s": (0.0, 0.7),
    "KQo": (0.2, 0.7), "KJo": (0.0, 0.7), "KTo": (0.0, 0.5),
    "QJs": (0.0, 0.9), "QTs": (0.0, 0.8), "Q9s": (0.0, 0.6),
    "JTs": (0.0, 0.9), "J9s": (0.0, 0.7),
    "T9s": (0.0, 0.8), "T8s": (0.0, 0.6),
    "98s": (0.0, 0.7), "87s": (0.0, 0.6), "76s": (0.0, 0.5), "65s": (0.0, 0.4),
    "QJo": (0.0, 0.6), "JTo": (0.0, 0.5),
}

_SB_VR = {
    # OOP postflop → fewer calls, heavier 3bet-or-fold
    "AA": (1.0, 0.0), "KK": (1.0, 0.0), "QQ": (1.0, 0.0), "JJ": (0.7, 0.0),
    "TT": (0.4, 0.0), "99": (0.2, 0.0), "88": (0.1, 0.0),
    "AKs": (1.0, 0.0), "AQs": (0.9, 0.0), "AJs": (0.5, 0.0), "ATs": (0.3, 0.0),
    "A5s": (0.7, 0.0), "A4s": (0.5, 0.0), "A3s": (0.4, 0.0), "A2s": (0.3, 0.0),
    "AKo": (1.0, 0.0), "AQo": (0.7, 0.0), "AJo": (0.2, 0.0),
    "KQs": (0.5, 0.0), "KJs": (0.2, 0.0),
    "KQo": (0.3, 0.0),
    "QJs": (0.1, 0.0),
}

_BB_VR = {
    # Wider defends — already invested 1BB, last to act preflop
    "AA": (1.0, 0.0), "KK": (1.0, 0.0), "QQ": (0.8, 0.2), "JJ": (0.5, 0.5),
    "TT": (0.2, 0.8), "99": (0.1, 0.9), "88": (0.0, 1.0), "77": (0.0, 0.9),
    "66": (0.0, 0.8), "55": (0.0, 0.7), "44": (0.0, 0.6), "33": (0.0, 0.5), "22": (0.0, 0.5),
    "AKs": (1.0, 0.0), "AQs": (0.7, 0.3), "AJs": (0.4, 0.6), "ATs": (0.2, 0.8),
    "A9s": (0.1, 0.9), "A8s": (0.0, 0.9), "A7s": (0.0, 0.8), "A6s": (0.0, 0.7),
    "A5s": (0.5, 0.0), "A4s": (0.4, 0.0), "A3s": (0.3, 0.0), "A2s": (0.2, 0.0),
    "AKo": (1.0, 0.0), "AQo": (0.5, 0.5), "AJo": (0.2, 0.8), "ATo": (0.0, 0.8),
    "A9o": (0.0, 0.7), "A8o": (0.0, 0.6), "A7o": (0.0, 0.5),
    "KQs": (0.4, 0.6), "KJs": (0.1, 0.9), "KTs": (0.0, 0.9), "K9s": (0.0, 0.8),
    "K8s": (0.0, 0.7), "K7s": (0.0, 0.6),
    "KQo": (0.2, 0.8), "KJo": (0.0, 0.8), "KTo": (0.0, 0.7), "K9o": (0.0, 0.5),
    "QJs": (0.1, 0.9), "QTs": (0.0, 0.9), "Q9s": (0.0, 0.8), "Q8s": (0.0, 0.6),
    "QJo": (0.0, 0.7), "QTo": (0.0, 0.6),
    "JTs": (0.1, 0.9), "J9s": (0.0, 0.8), "J8s": (0.0, 0.7),
    "JTo": (0.0, 0.6),
    "T9s": (0.0, 0.9), "T8s": (0.0, 0.7),
    "T9o": (0.0, 0.5),
    "98s": (0.0, 0.8), "97s": (0.0, 0.7),
    "87s": (0.0, 0.7), "86s": (0.0, 0.5),
    "76s": (0.0, 0.7), "75s": (0.0, 0.5),
    "65s": (0.0, 0.6), "64s": (0.0, 0.4),
    "54s": (0.0, 0.6), "53s": (0.0, 0.4),
}

_VS_RAISE = {
    "UTG":     _vs(_UTG_VR),
    "HJ":      _vs(_HJ_VR),
    "CO":      _vs(_CO_VR),
    "BTN":     _vs(_BTN_VR),
    "SB":      _vs(_SB_VR),
    "BB":      _vs(_BB_VR),
    "MP":      _vs(_HJ_VR),
    "unknown": _vs(_CO_VR),
}

_FOLD = (0.0, 0.0, 1.0)

# ── Short-handed adjustments ──────────────────────────────────────────────────
# Fewer players = wider ranges. Applied as multipliers on listed frequencies,
# with a floor open freq for unlisted hands at very short tables.

_SH_RFI_BOOST  = {6: 1.00, 5: 1.10, 4: 1.20, 3: 1.35, 2: 1.55}
_SH_RFI_FLOOR  = {6: 0.00, 5: 0.00, 4: 0.05, 3: 0.10, 2: 0.10}
_SH_CALL_BOOST = {6: 1.00, 5: 1.05, 4: 1.10, 3: 1.20, 2: 1.35}


def lookup(hole_cards: list, position: str, facing_raise: bool,
           num_players: int = 6) -> tuple:
    """
    Return (raise_freq, call_freq, fold_freq) for the given hand and position.

    facing_raise: True when to_call > big_blind (someone already raised).
    num_players:  Total players at the table. Fewer players → wider ranges.
    """
    key = hand_key(hole_cards)
    np  = max(2, min(num_players, 6))
    table = _VS_RAISE.get(position, _VS_RAISE["unknown"]) if facing_raise \
            else _RFI.get(position, _RFI["unknown"])
    base = table.get(key, None)

    if facing_raise:
        if base is None:
            return _FOLD
        r, c, f = base
        boost = _SH_CALL_BOOST.get(np, 1.0)
        if boost > 1.0 and c > 0:
            new_c = min(c * boost, 1.0 - r)
            return (r, round(new_c, 4), round(max(0.0, 1.0 - r - new_c), 4))
        return base
    else:
        boost = _SH_RFI_BOOST.get(np, 1.0)
        floor = _SH_RFI_FLOOR.get(np, 0.0)
        if base is None:
            if floor > 0.0:
                return (floor, 0.0, round(1.0 - floor, 4))
            return _FOLD
        r, _c, _f = base
        if boost > 1.0:
            new_r = min(r * boost, 1.0)
            return (round(new_r, 4), 0.0, round(1.0 - new_r, 4))
        return base
