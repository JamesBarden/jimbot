"""
Shared helpers for turn_heuristic and river_heuristic.

Anything that varies between streets (base frequency tables, villain-fold
adjustments, SPR thresholds, marginal-hand sets) stays in the per-street
file. Only constants and pure utility functions live here.
"""

IP_POSITIONS = frozenset({"BTN", "CO", "HJ"})

STRONG = frozenset({"monster", "full_house", "flush", "straight", "set"})

FOLD_SENSITIVE = frozenset({
    "trips", "two_pair", "overpair", "top_pair_top", "top_pair_weak",
    "middle_pair", "bottom_pair", "combo_draw", "draw", "weak_draw",
})

# (fold_mult_value, fold_mult_air, raise_adj_strong, raise_adj_air)
TIER_ADJ = {
    "small":   (0.60, 0.82, -0.05,  0.00),
    "medium":  (1.00, 1.00,  0.00,  0.00),
    "large":   (1.30, 1.10, +0.05, -0.05),
    "overbet": (1.70, 1.30, +0.12, -0.10),
}


def bet_tier(bet_fraction: float) -> str:
    if bet_fraction <= 0.35: return "small"
    if bet_fraction <= 0.75: return "medium"
    if bet_fraction <= 1.15: return "large"
    return "overbet"


def clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def normalize(r: float, c: float, f: float) -> tuple:
    total = r + c + f
    if total <= 0:
        return (0.0, 0.0, 1.0)
    return (r / total, c / total, f / total)
