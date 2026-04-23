"""
Hand range definitions for opponent modelling.

Ranges are defined as sets of canonical hand types: (high_rank, low_rank, suited).
Pairs use (rank, rank, False). Each tier is a superset of the one above it.

Tier selection guide (heads-up):
  premium  ~5%  — 3-bet/4-bet range, facing a huge raise
  tight   ~20%  — standard open-raise or calling a 3-bet
  medium  ~40%  — calling a standard raise
  wide    ~65%  — open-limping or facing a min-raise
  random  100%  — no information
"""

from treys import Card as TreysCard

RANKS = "23456789TJQKA"   # index 0=2, 12=A
SUITS = "shdc"


# ---------------------------------------------------------------------------
# Range definitions
# ---------------------------------------------------------------------------

def _p(*rs):
    """Pair hand types."""
    return {(r, r, False) for r in rs}

def _s(*ps):
    """Specific suited hand types."""
    return {(h, l, True) for h, l in ps}

def _o(*ps):
    """Specific offsuit hand types."""
    return {(h, l, False) for h, l in ps}

def _sr(high: str, low_lo: str, low_hi: str):
    """Suited range: all high-X suited where X runs from low_lo to low_hi."""
    hi_idx = RANKS.index(high)
    return {
        (high, RANKS[i], True)
        for i in range(RANKS.index(low_lo), RANKS.index(low_hi) + 1)
        if i < hi_idx
    }

def _or(high: str, low_lo: str, low_hi: str):
    """Offsuit range."""
    hi_idx = RANKS.index(high)
    return {
        (high, RANKS[i], False)
        for i in range(RANKS.index(low_lo), RANKS.index(low_hi) + 1)
        if i < hi_idx
    }


PREMIUM: set = (
    _p('A', 'K', 'Q', 'J', 'T') |
    _s(('A','K'), ('A','Q'), ('A','J'), ('A','T')) |
    _o(('A','K'), ('A','Q'))
)

TIGHT: set = PREMIUM | (
    _p('9', '8', '7') |
    _s(('K','Q'), ('K','J'), ('K','T'), ('Q','J'), ('Q','T'), ('J','T')) |
    _o(('A','J'), ('A','T'), ('K','Q'), ('K','J'))
)

MEDIUM: set = TIGHT | (
    _p('6', '5', '4', '3', '2') |
    _sr('A', '2', '9') |               # A2s–A9s
    _sr('K', '5', '9') |               # K5s–K9s
    _sr('Q', '7', '9') |               # Q7s–Q9s
    _sr('J', '7', '9') |               # J7s–J9s
    _s(('T','9'), ('9','8'), ('8','7'), ('7','6'), ('6','5'), ('5','4')) |
    _or('A', '5', '9') |               # A5o–A9o
    _o(('K','T'), ('Q','J'), ('Q','T'), ('J','T'))
)

WIDE: set = MEDIUM | (
    _sr('K', '2', '4') |               # K2s–K4s
    _sr('Q', '4', '6') |               # Q4s–Q6s
    _sr('J', '4', '6') |               # J4s–J6s
    _sr('T', '6', '8') |               # T6s–T8s
    _s(('9','7'), ('8','6'), ('7','5'), ('6','4'), ('5','3'), ('4','3')) |
    _or('A', '2', '4') |               # A2o–A4o
    _or('K', '7', '9') |               # K7o–K9o
    _o(('Q','9'), ('J','9'), ('T','9'), ('9','8'))
)

# All 169 canonical hand types (100%)
RANDOM: set = set()
for _i in range(len(RANKS)):
    for _j in range(_i, len(RANKS)):
        _r1, _r2 = RANKS[_j], RANKS[_i]   # higher rank first
        if _r1 == _r2:
            RANDOM.add((_r1, _r2, False))
        else:
            RANDOM.add((_r1, _r2, True))
            RANDOM.add((_r1, _r2, False))

TIERS = {
    "premium": PREMIUM,
    "tight":   TIGHT,
    "medium":  MEDIUM,
    "wide":    WIDE,
    "random":  RANDOM,
}


# ---------------------------------------------------------------------------
# Combo expansion
# ---------------------------------------------------------------------------

def _expand(hand_types: set) -> list[tuple[str, str]]:
    """Expand canonical hand types into all specific (card_str, card_str) combos."""
    combos = []
    suits = list(SUITS)
    for (r1, r2, suited) in hand_types:
        if r1 == r2:                           # pair — 6 combos
            for i in range(4):
                for j in range(i + 1, 4):
                    combos.append((r1 + suits[i], r2 + suits[j]))
        elif suited:                            # suited — 4 combos
            for s in suits:
                combos.append((r1 + s, r2 + s))
        else:                                   # offsuit — 12 combos
            for s1 in suits:
                for s2 in suits:
                    if s1 != s2:
                        combos.append((r1 + s1, r2 + s2))
    return combos


# Pre-compute treys int combos for each tier (excluding nothing — filtering happens at runtime)
_EXPANDED: dict[str, list[tuple[int, int]]] = {
    name: [(TreysCard.new(c1), TreysCard.new(c2)) for c1, c2 in _expand(types)]
    for name, types in TIERS.items()
}


def get_combos(tier: str, exclude: set[int]) -> list[tuple[int, int]]:
    """
    Return all (treys_card1, treys_card2) combos for the given tier,
    with any cards in `exclude` (our hole cards + board) removed.
    """
    return [
        (c1, c2)
        for c1, c2 in _EXPANDED.get(tier, _EXPANDED["random"])
        if c1 not in exclude and c2 not in exclude
    ]


def tier_size(tier: str) -> int:
    return len(_EXPANDED.get(tier, _EXPANDED["random"]))
