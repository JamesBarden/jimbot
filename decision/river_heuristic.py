"""
River street pseudo-GTO heuristics.

Key differences from turn_heuristic:
  - No semi-bluffs: draws have resolved — draw hands become made hands or air.
  - Higher fold frequencies for marginal holdings (no future streets to improve).
  - Air bluffs at 20-25% (polarised river betting strategy).
  - trips_board added as an extra board texture (river can boat up a paired turn).
  - SPR matters less on the river — nearly always low by this point, but kept.

Strategy layers applied in order:
  1. Base frequencies — hand_class × river_texture table
  2. Position         — IP (BTN/CO/HJ) slightly more aggressive
  3. SPR              — only large effects at extremes
  4. Villain tier     — tighter villain warrants more caution on marginal hands

Returns (raise_freq, call_freq, fold_freq) summing to 1.0.
"""

from .hand_classifier import classify, river_card_texture
from .heuristic_utils import (
    IP_POSITIONS, STRONG, FOLD_SENSITIVE, TIER_ADJ,
    bet_tier, clamp, normalize,
)

_TEXTURES = ("blank", "flush_complete", "straight_complete",
             "pair_board", "trips_board", "overcard")

# ── Base bet frequencies when no bet is facing ───────────────────────────────
# raise_freq = bet frequency; check_freq = 1 - raise_freq; fold_freq = 0
# Indexed: hand_class → {river_texture: bet_freq}

_FREE = {
    "monster":       {"blank": 0.95, "flush_complete": 0.95, "straight_complete": 0.95, "pair_board": 0.95, "trips_board": 0.95, "overcard": 0.95},
    "full_house":    {"blank": 0.92, "flush_complete": 0.92, "straight_complete": 0.92, "pair_board": 0.95, "trips_board": 0.95, "overcard": 0.92},
    "flush":         {"blank": 0.80, "flush_complete": 0.90, "straight_complete": 0.80, "pair_board": 0.80, "trips_board": 0.80, "overcard": 0.80},
    "straight":      {"blank": 0.78, "flush_complete": 0.60, "straight_complete": 0.85, "pair_board": 0.70, "trips_board": 0.70, "overcard": 0.78},
    "set":           {"blank": 0.75, "flush_complete": 0.55, "straight_complete": 0.55, "pair_board": 0.80, "trips_board": 0.85, "overcard": 0.75},
    "trips":         {"blank": 0.65, "flush_complete": 0.42, "straight_complete": 0.42, "pair_board": 0.60, "trips_board": 0.70, "overcard": 0.62},
    "two_pair":      {"blank": 0.60, "flush_complete": 0.35, "straight_complete": 0.35, "pair_board": 0.50, "trips_board": 0.60, "overcard": 0.45},
    "overpair":      {"blank": 0.60, "flush_complete": 0.35, "straight_complete": 0.35, "pair_board": 0.50, "trips_board": 0.55, "overcard": 0.40},
    "top_pair_top":  {"blank": 0.55, "flush_complete": 0.30, "straight_complete": 0.30, "pair_board": 0.45, "trips_board": 0.50, "overcard": 0.35},
    "top_pair_weak": {"blank": 0.40, "flush_complete": 0.20, "straight_complete": 0.20, "pair_board": 0.30, "trips_board": 0.35, "overcard": 0.20},
    "middle_pair":   {"blank": 0.20, "flush_complete": 0.10, "straight_complete": 0.10, "pair_board": 0.15, "trips_board": 0.20, "overcard": 0.10},
    "bottom_pair":   {"blank": 0.10, "flush_complete": 0.05, "straight_complete": 0.05, "pair_board": 0.10, "trips_board": 0.10, "overcard": 0.05},
    # draws resolved — no more combo_draw/draw/weak_draw on river
    "combo_draw":    {"blank": 0.20, "flush_complete": 0.10, "straight_complete": 0.10, "pair_board": 0.15, "trips_board": 0.15, "overcard": 0.15},
    "draw":          {"blank": 0.20, "flush_complete": 0.10, "straight_complete": 0.10, "pair_board": 0.15, "trips_board": 0.15, "overcard": 0.15},
    "weak_draw":     {"blank": 0.20, "flush_complete": 0.10, "straight_complete": 0.10, "pair_board": 0.15, "trips_board": 0.15, "overcard": 0.15},
    "air":           {"blank": 0.22, "flush_complete": 0.18, "straight_complete": 0.18, "pair_board": 0.12, "trips_board": 0.12, "overcard": 0.20},
}

# ── Base frequencies when facing a bet ───────────────────────────────────────
# (raise_freq, call_freq, fold_freq)
# River: no more cards to come → fold more with marginal hands,
#        raise only for value or polarised bluff-raise on right textures.

_FACED = {
    "monster":       {"blank": (0.65, 0.35, 0.00), "flush_complete": (0.65, 0.35, 0.00), "straight_complete": (0.65, 0.35, 0.00), "pair_board": (0.65, 0.35, 0.00), "trips_board": (0.65, 0.35, 0.00), "overcard": (0.65, 0.35, 0.00)},
    "full_house":    {"blank": (0.60, 0.40, 0.00), "flush_complete": (0.60, 0.40, 0.00), "straight_complete": (0.60, 0.40, 0.00), "pair_board": (0.65, 0.35, 0.00), "trips_board": (0.65, 0.35, 0.00), "overcard": (0.60, 0.40, 0.00)},
    "flush":         {"blank": (0.50, 0.50, 0.00), "flush_complete": (0.55, 0.45, 0.00), "straight_complete": (0.50, 0.50, 0.00), "pair_board": (0.50, 0.50, 0.00), "trips_board": (0.50, 0.50, 0.00), "overcard": (0.50, 0.50, 0.00)},
    "straight":      {"blank": (0.45, 0.55, 0.00), "flush_complete": (0.30, 0.55, 0.15), "straight_complete": (0.50, 0.50, 0.00), "pair_board": (0.40, 0.55, 0.05), "trips_board": (0.35, 0.55, 0.10), "overcard": (0.45, 0.55, 0.00)},
    "set":           {"blank": (0.40, 0.60, 0.00), "flush_complete": (0.25, 0.55, 0.20), "straight_complete": (0.25, 0.55, 0.20), "pair_board": (0.45, 0.55, 0.00), "trips_board": (0.50, 0.50, 0.00), "overcard": (0.40, 0.60, 0.00)},
    "trips":         {"blank": (0.28, 0.57, 0.15), "flush_complete": (0.12, 0.40, 0.48), "straight_complete": (0.12, 0.40, 0.48), "pair_board": (0.28, 0.52, 0.20), "trips_board": (0.30, 0.52, 0.18), "overcard": (0.20, 0.50, 0.30)},
    "two_pair":      {"blank": (0.20, 0.55, 0.25), "flush_complete": (0.10, 0.35, 0.55), "straight_complete": (0.10, 0.35, 0.55), "pair_board": (0.15, 0.45, 0.40), "trips_board": (0.20, 0.50, 0.30), "overcard": (0.10, 0.45, 0.45)},
    "overpair":      {"blank": (0.20, 0.55, 0.25), "flush_complete": (0.10, 0.30, 0.60), "straight_complete": (0.10, 0.30, 0.60), "pair_board": (0.15, 0.45, 0.40), "trips_board": (0.15, 0.45, 0.40), "overcard": (0.10, 0.40, 0.50)},
    "top_pair_top":  {"blank": (0.10, 0.55, 0.35), "flush_complete": (0.05, 0.25, 0.70), "straight_complete": (0.05, 0.25, 0.70), "pair_board": (0.10, 0.40, 0.50), "trips_board": (0.10, 0.40, 0.50), "overcard": (0.05, 0.35, 0.60)},
    "top_pair_weak": {"blank": (0.05, 0.45, 0.50), "flush_complete": (0.00, 0.20, 0.80), "straight_complete": (0.00, 0.20, 0.80), "pair_board": (0.05, 0.30, 0.65), "trips_board": (0.05, 0.30, 0.65), "overcard": (0.00, 0.25, 0.75)},
    "middle_pair":   {"blank": (0.00, 0.30, 0.70), "flush_complete": (0.00, 0.10, 0.90), "straight_complete": (0.00, 0.10, 0.90), "pair_board": (0.00, 0.20, 0.80), "trips_board": (0.00, 0.25, 0.75), "overcard": (0.00, 0.15, 0.85)},
    "bottom_pair":   {"blank": (0.00, 0.15, 0.85), "flush_complete": (0.00, 0.05, 0.95), "straight_complete": (0.00, 0.05, 0.95), "pair_board": (0.00, 0.10, 0.90), "trips_board": (0.00, 0.15, 0.85), "overcard": (0.00, 0.10, 0.90)},
    # unimproved draws → treated as air on river
    "combo_draw":    {"blank": (0.00, 0.10, 0.90), "flush_complete": (0.00, 0.05, 0.95), "straight_complete": (0.00, 0.05, 0.95), "pair_board": (0.00, 0.10, 0.90), "trips_board": (0.00, 0.10, 0.90), "overcard": (0.00, 0.10, 0.90)},
    "draw":          {"blank": (0.00, 0.10, 0.90), "flush_complete": (0.00, 0.05, 0.95), "straight_complete": (0.00, 0.05, 0.95), "pair_board": (0.00, 0.10, 0.90), "trips_board": (0.00, 0.10, 0.90), "overcard": (0.00, 0.10, 0.90)},
    "weak_draw":     {"blank": (0.00, 0.05, 0.95), "flush_complete": (0.00, 0.05, 0.95), "straight_complete": (0.00, 0.05, 0.95), "pair_board": (0.00, 0.05, 0.95), "trips_board": (0.00, 0.05, 0.95), "overcard": (0.00, 0.05, 0.95)},
    "air":           {"blank": (0.12, 0.05, 0.83), "flush_complete": (0.08, 0.05, 0.87), "straight_complete": (0.08, 0.05, 0.87), "pair_board": (0.05, 0.05, 0.90), "trips_board": (0.05, 0.05, 0.90), "overcard": (0.10, 0.05, 0.85)},
}

# ── Adjustment tables ─────────────────────────────────────────────────────────

_VILLAIN_FOLD_ADJ = {
    "premium": +0.12,
    "tight":   +0.08,
    "medium":   0.00,
    "wide":    -0.08,
    "random":  -0.04,
}

_MARGINAL = {"trips", "two_pair", "overpair", "top_pair_top", "top_pair_weak",
             "middle_pair", "bottom_pair"}

# SPR → (raise_adj, fold_adj). Thresholds tuned per-street.
def _spr_adj(spr: float) -> tuple:
    if spr < 2:
        return (+0.06, -0.06)   # committed — less reason to fold
    if spr > 8:
        return (-0.04, +0.04)   # deep stack reaching river is unusual; tread carefully
    return (0.0, 0.0)


# ── Public interface ──────────────────────────────────────────────────────────

def query(
    hole_cards:   list,
    board_cards:  list,
    position:     str,
    villain_tier: str,
    facing_bet:   bool,
    spr:          float,
    bet_fraction: float = 0.0,
    context       = None,
) -> tuple:
    """
    Return (raise_freq, call_freq, fold_freq) for the river street.

    hole_cards   : list[Card], length 2
    board_cards  : list[Card], length 5 (includes river card)
    position     : 'BTN', 'CO', 'HJ', 'SB', 'BB', 'UTG', 'unknown', etc.
    villain_tier : 'premium', 'tight', 'medium', 'wide', 'random'
    facing_bet   : True if there is a bet to call/raise/fold
    spr          : effective stack / pot
    bet_fraction : villain's bet size as fraction of pot (0 if not facing a bet)
    context      : optional HandContext — triple-barrel logic uses it
    """
    hand_class = classify(hole_cards, board_cards)
    tex        = river_card_texture(board_cards)
    is_ip      = position in IP_POSITIONS

    # 1. Base frequencies
    if facing_bet:
        row = _FACED.get(hand_class, _FACED["air"])
        r, c, f = row.get(tex, row["blank"])
    else:
        bet_freq = _FREE.get(hand_class, _FREE["air"]).get(tex, 0.15)
        r, c, f  = bet_freq, 1.0 - bet_freq, 0.0

    # 2. Position
    pos_raise = +0.04 if is_ip else -0.04
    r = clamp(r + pos_raise)

    # 3. SPR
    spr_r, spr_f = _spr_adj(spr)
    if not facing_bet and hand_class in _MARGINAL:
        spr_r = min(spr_r, 0.0)  # low SPR prices you in to call, not a reason to thin-bet weak hands
    r = clamp(r + spr_r)
    if facing_bet:
        f = clamp(f + spr_f)

    # 4. Villain tier (marginal hands only)
    if facing_bet and hand_class in _MARGINAL:
        fold_delta = _VILLAIN_FOLD_ADJ.get(villain_tier, 0.0)
        f = clamp(f + fold_delta)

    # 5. Villain bet sizing — tighten vs large bets, loosen vs small bets
    if facing_bet and bet_fraction > 0:
        tier                                 = bet_tier(bet_fraction)
        fold_val, fold_air, raise_s, raise_a = TIER_ADJ[tier]
        if hand_class in FOLD_SENSITIVE:
            f = clamp(f * fold_val)
        elif hand_class == "air":
            f = clamp(f * fold_air)
            r = clamp(r + raise_a)
        if hand_class in STRONG:
            r = clamp(r + raise_s)

    # 6. Cross-street context: triple-barrel logic. We only barrel the river
    #    if we've fired both prior streets (flop + turn) and villain kept
    #    calling — otherwise the line makes no sense.
    if context is not None and not facing_bet:
        if context.is_double_barrel_spot("river") and context.villain_check_called_last_street("river"):
            barrel_bonus = 0.0
            if hand_class in STRONG:                 barrel_bonus = +0.10   # thin value
            elif hand_class == "air":                 barrel_bonus = +0.12   # pure bluff — rare but real
            elif hand_class in _MARGINAL:             barrel_bonus = +0.04
            r = clamp(r + barrel_bonus)

    # 7. Stab bonus: HU in position, villain checked to us on the river.
    #    Smaller magnitudes than turn — fold equity is lower on river since
    #    villain has narrower range, and unimproved draws have no equity.
    #    Still: villain showing weakness in position is profitable to attack.
    if (context is not None and not facing_bet and is_ip
            and getattr(context, "num_opponents", 0) == 1):
        stab_bonus = 0.0
        if   hand_class == "air":                            stab_bonus = +0.12
        elif hand_class in ("weak_draw", "draw", "combo_draw"): stab_bonus = +0.10
        elif hand_class in _MARGINAL:                        stab_bonus = +0.05
        elif hand_class in STRONG:                          stab_bonus = +0.04   # thin value
        r = clamp(r + stab_bonus)

    # Rebalance call, then normalise
    if facing_bet:
        c = max(0.0, 1.0 - r - f)
    else:
        c = max(0.0, 1.0 - r)
        f = 0.0

    return normalize(r, c, f)
