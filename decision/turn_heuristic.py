"""
Turn street pseudo-GTO heuristics.

Strategy is built from four layers applied in order:
  1. Base frequencies — hand_class × turn_texture table
  2. Position         — IP (BTN/CO/HJ) slightly more aggressive
  3. SPR              — low SPR commits more; high SPR tightens up
  4. Villain tier     — tighter villain warrants more caution on marginal hands

Returns (raise_freq, call_freq, fold_freq) summing to 1.0.

When no bet is facing (can check): fold_freq is always 0; raise=bet, call=check.
When facing a bet: full three-way split.

Hand classes come from hand_classifier.classify() evaluated on the 4-card board,
so if a flush draw completed for us the hand_class is already 'flush', not 'draw'.
"""

from .hand_classifier import classify, turn_card_texture
from .heuristic_utils import (
    IP_POSITIONS, STRONG, FOLD_SENSITIVE, TIER_ADJ,
    bet_tier, clamp, normalize,
)

# ── Base bet frequencies (no bet facing) ─────────────────────────────────────
# raise_freq = probability to bet; check_freq = 1 - raise_freq; fold_freq = 0
# Indexed: hand_class → {turn_texture: bet_freq}

_FREE = {
    "monster":       {"blank": 0.95, "flush_complete": 0.95, "straight_complete": 0.95, "pair_board": 0.95, "overcard": 0.95},
    "full_house":    {"blank": 0.90, "flush_complete": 0.90, "straight_complete": 0.90, "pair_board": 0.95, "overcard": 0.90},
    "flush":         {"blank": 0.80, "flush_complete": 0.95, "straight_complete": 0.80, "pair_board": 0.80, "overcard": 0.80},
    "straight":      {"blank": 0.80, "flush_complete": 0.70, "straight_complete": 0.90, "pair_board": 0.75, "overcard": 0.80},
    "set":           {"blank": 0.75, "flush_complete": 0.60, "straight_complete": 0.60, "pair_board": 0.85, "overcard": 0.75},
    "trips":         {"blank": 0.68, "flush_complete": 0.50, "straight_complete": 0.50, "pair_board": 0.65, "overcard": 0.65},
    "two_pair":      {"blank": 0.65, "flush_complete": 0.45, "straight_complete": 0.45, "pair_board": 0.50, "overcard": 0.50},
    "overpair":      {"blank": 0.65, "flush_complete": 0.45, "straight_complete": 0.45, "pair_board": 0.55, "overcard": 0.45},
    "top_pair_top":  {"blank": 0.60, "flush_complete": 0.40, "straight_complete": 0.40, "pair_board": 0.50, "overcard": 0.40},
    "top_pair_weak": {"blank": 0.45, "flush_complete": 0.30, "straight_complete": 0.30, "pair_board": 0.35, "overcard": 0.30},
    "middle_pair":   {"blank": 0.25, "flush_complete": 0.15, "straight_complete": 0.15, "pair_board": 0.20, "overcard": 0.15},
    "bottom_pair":   {"blank": 0.15, "flush_complete": 0.10, "straight_complete": 0.10, "pair_board": 0.10, "overcard": 0.10},
    "combo_draw":    {"blank": 0.55, "flush_complete": 0.30, "straight_complete": 0.35, "pair_board": 0.45, "overcard": 0.45},
    "draw":          {"blank": 0.35, "flush_complete": 0.20, "straight_complete": 0.25, "pair_board": 0.30, "overcard": 0.30},
    "weak_draw":     {"blank": 0.20, "flush_complete": 0.15, "straight_complete": 0.15, "pair_board": 0.15, "overcard": 0.15},
    "air":           {"blank": 0.20, "flush_complete": 0.15, "straight_complete": 0.15, "pair_board": 0.10, "overcard": 0.15},
}

# ── Base frequencies when facing a bet ───────────────────────────────────────
# (raise_freq, call_freq, fold_freq)

_FACED = {
    "monster":       {"blank": (0.60, 0.40, 0.00), "flush_complete": (0.60, 0.40, 0.00), "straight_complete": (0.60, 0.40, 0.00), "pair_board": (0.60, 0.40, 0.00), "overcard": (0.60, 0.40, 0.00)},
    "full_house":    {"blank": (0.55, 0.45, 0.00), "flush_complete": (0.55, 0.45, 0.00), "straight_complete": (0.55, 0.45, 0.00), "pair_board": (0.60, 0.40, 0.00), "overcard": (0.55, 0.45, 0.00)},
    "flush":         {"blank": (0.50, 0.50, 0.00), "flush_complete": (0.55, 0.45, 0.00), "straight_complete": (0.50, 0.50, 0.00), "pair_board": (0.50, 0.50, 0.00), "overcard": (0.50, 0.50, 0.00)},
    "straight":      {"blank": (0.50, 0.50, 0.00), "flush_complete": (0.40, 0.55, 0.05), "straight_complete": (0.55, 0.45, 0.00), "pair_board": (0.45, 0.50, 0.05), "overcard": (0.50, 0.50, 0.00)},
    "set":           {"blank": (0.45, 0.55, 0.00), "flush_complete": (0.35, 0.60, 0.05), "straight_complete": (0.35, 0.60, 0.05), "pair_board": (0.50, 0.50, 0.00), "overcard": (0.45, 0.55, 0.00)},
    "trips":         {"blank": (0.35, 0.57, 0.08), "flush_complete": (0.22, 0.55, 0.23), "straight_complete": (0.22, 0.55, 0.23), "pair_board": (0.35, 0.55, 0.10), "overcard": (0.28, 0.55, 0.17)},
    "two_pair":      {"blank": (0.30, 0.60, 0.10), "flush_complete": (0.20, 0.55, 0.25), "straight_complete": (0.20, 0.55, 0.25), "pair_board": (0.25, 0.55, 0.20), "overcard": (0.20, 0.60, 0.20)},
    "overpair":      {"blank": (0.30, 0.60, 0.10), "flush_complete": (0.20, 0.50, 0.30), "straight_complete": (0.20, 0.50, 0.30), "pair_board": (0.25, 0.55, 0.20), "overcard": (0.20, 0.55, 0.25)},
    "top_pair_top":  {"blank": (0.20, 0.65, 0.15), "flush_complete": (0.10, 0.50, 0.40), "straight_complete": (0.10, 0.50, 0.40), "pair_board": (0.15, 0.55, 0.30), "overcard": (0.10, 0.55, 0.35)},
    "top_pair_weak": {"blank": (0.10, 0.60, 0.30), "flush_complete": (0.05, 0.40, 0.55), "straight_complete": (0.05, 0.40, 0.55), "pair_board": (0.10, 0.45, 0.45), "overcard": (0.05, 0.45, 0.50)},
    "middle_pair":   {"blank": (0.05, 0.45, 0.50), "flush_complete": (0.00, 0.25, 0.75), "straight_complete": (0.00, 0.25, 0.75), "pair_board": (0.05, 0.35, 0.60), "overcard": (0.00, 0.30, 0.70)},
    "bottom_pair":   {"blank": (0.00, 0.30, 0.70), "flush_complete": (0.00, 0.15, 0.85), "straight_complete": (0.00, 0.15, 0.85), "pair_board": (0.00, 0.20, 0.80), "overcard": (0.00, 0.20, 0.80)},
    "combo_draw":    {"blank": (0.30, 0.65, 0.05), "flush_complete": (0.10, 0.35, 0.55), "straight_complete": (0.15, 0.45, 0.40), "pair_board": (0.20, 0.65, 0.15), "overcard": (0.25, 0.60, 0.15)},
    "draw":          {"blank": (0.15, 0.65, 0.20), "flush_complete": (0.05, 0.20, 0.75), "straight_complete": (0.10, 0.30, 0.60), "pair_board": (0.10, 0.55, 0.35), "overcard": (0.10, 0.55, 0.35)},
    "weak_draw":     {"blank": (0.05, 0.40, 0.55), "flush_complete": (0.00, 0.15, 0.85), "straight_complete": (0.05, 0.20, 0.75), "pair_board": (0.00, 0.30, 0.70), "overcard": (0.00, 0.30, 0.70)},
    "air":           {"blank": (0.10, 0.20, 0.70), "flush_complete": (0.05, 0.10, 0.85), "straight_complete": (0.05, 0.10, 0.85), "pair_board": (0.00, 0.15, 0.85), "overcard": (0.05, 0.15, 0.80)},
}

# ── Adjustment tables ─────────────────────────────────────────────────────────

# Villain tier → delta added to fold_freq for marginal hands (facing a bet)
_VILLAIN_FOLD_ADJ = {
    "premium": +0.10,
    "tight":   +0.06,
    "medium":   0.00,
    "wide":    -0.06,
    "random":  -0.03,
}

# Hands where villain-range adjustments actually apply (not strong, not air)
_MARGINAL = {"trips", "two_pair", "overpair", "top_pair_top", "top_pair_weak",
             "middle_pair", "draw", "combo_draw"}

# SPR → (raise_adj, fold_adj). Thresholds tuned per-street.
def _spr_adj(spr: float) -> tuple:
    if spr < 4:
        return (+0.08, -0.08)   # short stack — commit more aggressively
    if spr > 10:
        return (-0.05, +0.05)   # deep stack — tread carefully
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
    Return (raise_freq, call_freq, fold_freq) for the turn street.

    hole_cards   : list[Card], length 2
    board_cards  : list[Card], length 4 (includes turn card)
    position     : 'BTN', 'CO', 'HJ', 'SB', 'BB', 'UTG', 'unknown', etc.
    villain_tier : 'premium', 'tight', 'medium', 'wide', 'random'
    facing_bet   : True if there is a bet to call/raise/fold
    spr          : effective stack / pot
    bet_fraction : villain's bet size as fraction of pot (0 if not facing a bet)
    context      : optional HandContext for cross-street awareness
                   (PFA identity, whether we cbet the flop, villain check-called)
    """
    hand_class = classify(hole_cards, board_cards)
    tex        = turn_card_texture(board_cards)
    is_ip      = position in IP_POSITIONS

    # 1. Base frequencies
    if facing_bet:
        row = _FACED.get(hand_class, _FACED["air"])
        r, c, f = row.get(tex, row["blank"])
    else:
        bet_freq = _FREE.get(hand_class, _FREE["air"]).get(tex, 0.20)
        r, c, f  = bet_freq, 1.0 - bet_freq, 0.0

    # 2. Position
    pos_raise = +0.05 if is_ip else -0.05
    r = clamp(r + pos_raise)

    # 3. SPR
    spr_r, spr_f = _spr_adj(spr)
    r = clamp(r + spr_r)
    if facing_bet:
        f = clamp(f + spr_f)

    # 4. Villain tier (marginal hands only)
    if facing_bet and hand_class in _MARGINAL:
        fold_delta = _VILLAIN_FOLD_ADJ.get(villain_tier, 0.0)
        f = clamp(f + fold_delta)

    # 5. Villain bet sizing — tighten vs large bets, loosen vs small bets
    if facing_bet and bet_fraction > 0:
        tier                               = bet_tier(bet_fraction)
        fold_val, fold_air, raise_s, raise_a = TIER_ADJ[tier]
        if hand_class in FOLD_SENSITIVE:
            f = clamp(f * fold_val)
        elif hand_class == "air":
            f = clamp(f * fold_air)
            r = clamp(r + raise_a)
        if hand_class in STRONG:
            r = clamp(r + raise_s)

    # 6. Cross-street context: double-barrel / give-up logic
    #    Only meaningful when we already bet the flop — the classic spot where
    #    the street-by-street heuristic over-gives-up because it forgets we
    #    represented a hand. Bump barrel frequency when we're the PFA and
    #    villain check-called.
    if context is not None and not facing_bet:
        if context.is_double_barrel_spot("turn") and context.villain_check_called_last_street("turn"):
            # Good turn cards to barrel: overcards, scare cards (straight/flush complete)
            barrel_bonus = 0.0
            if hand_class in STRONG:                     barrel_bonus = +0.12
            elif hand_class in ("combo_draw", "draw"):    barrel_bonus = +0.20   # semi-bluff
            elif hand_class in ("weak_draw", "air"):      barrel_bonus = +0.15   # pure bluff
            elif hand_class in _MARGINAL:                 barrel_bonus = +0.06
            if tex in ("overcard", "flush_complete", "straight_complete") and hand_class in ("air", "weak_draw"):
                barrel_bonus += 0.08    # scare cards favour the preflop aggressor
            r = clamp(r + barrel_bonus)

    # 7. Stab bonus: HU in position, villain checked to us. Independent of
    #    barrel logic — addresses the "no betting initiative" leak where the
    #    bot checks back even when villain has shown weakness. Stacks with
    #    the barrel bonus when both apply (we c-bet flop AND villain checks
    #    again on the turn → triple-barrel territory).
    if (context is not None and not facing_bet and is_ip
            and getattr(context, "num_opponents", 0) == 1):
        stab_bonus = 0.0
        if   hand_class == "air":                     stab_bonus = +0.18
        elif hand_class == "weak_draw":               stab_bonus = +0.15
        elif hand_class in ("draw", "combo_draw"):    stab_bonus = +0.12
        elif hand_class in _MARGINAL:                 stab_bonus = +0.08
        elif hand_class in STRONG:                   stab_bonus = +0.05   # thin value
        r = clamp(r + stab_bonus)

    # Rebalance call to absorb adjustments, then normalise
    if facing_bet:
        c = max(0.0, 1.0 - r - f)
    else:
        c = max(0.0, 1.0 - r)
        f = 0.0

    return normalize(r, c, f)
