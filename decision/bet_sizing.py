"""
Bet and raise sizing.

Samples a pot fraction from {0.33, 0.66, 1.00, 1.50} based on hand strength,
board wetness, and whether we are opening (betting) or raising a bet.

Design principles
  - Polarised hands (nutted value + air bluffs) lean toward 100–150%.
  - Medium-strength merged hands lean toward 33–66%.
  - Raising a bet shifts all weights toward larger sizes vs betting first.
  - Wet boards (draws present) push toward larger sizes to charge opponents.

Preflop sizing handled separately via pick_preflop_size().
"""

import random
from .hand_classifier import board_texture, turn_card_texture, river_card_texture

_SIZES = (0.33, 0.66, 1.00, 1.50)

# ── Weight tables ─────────────────────────────────────────────────────────────
# Each entry: (w33, w66, w100, w150) — raw unnormalized weights.
# Adjusted by board wetness multiplier before sampling.

# Betting into an uncalled pot (merged + polarised range):
_BET_WEIGHTS: dict = {
    "monster":       ( 5, 20, 45, 30),
    "full_house":    ( 5, 25, 45, 25),
    "flush":         ( 5, 30, 45, 20),
    "straight":      (10, 35, 40, 15),
    "set":           (10, 30, 45, 15),
    "trips":         (12, 38, 38, 12),
    "two_pair":      (15, 45, 30, 10),
    "overpair":      (15, 50, 25, 10),
    "top_pair_top":  (20, 50, 22,  8),
    "top_pair_weak": (35, 45, 15,  5),
    "middle_pair":   (55, 35,  8,  2),
    "bottom_pair":   (65, 28,  5,  2),
    "combo_draw":    (10, 35, 40, 15),
    "draw":          (15, 40, 35, 10),
    "weak_draw":     (35, 40, 20,  5),
    "air":           (35, 35, 22,  8),  # block-bet leaning; large bluffs are too committal in low SPR
}

# Raising a bet (more polarised — shift toward larger sizes):
_RAISE_WEIGHTS: dict = {
    "monster":       ( 5, 15, 35, 45),
    "full_house":    ( 5, 18, 42, 35),
    "flush":         ( 5, 22, 48, 25),
    "straight":      ( 8, 28, 44, 20),
    "set":           (10, 30, 42, 18),
    "trips":         (13, 35, 37, 15),
    "two_pair":      (18, 42, 30, 10),
    "overpair":      (20, 45, 25, 10),
    "top_pair_top":  (25, 45, 25,  5),
    "top_pair_weak": (35, 45, 15,  5),
    "middle_pair":   (55, 35,  8,  2),
    "bottom_pair":   (70, 25,  4,  1),
    "combo_draw":    (10, 28, 42, 20),
    "draw":          (12, 35, 38, 15),
    "weak_draw":     (35, 40, 20,  5),
    "air":           (30, 35, 25, 10),  # smaller raise-bluffs; cap loss when called/3bet
}

# Multipliers applied element-wise before normalising.
# Wet boards suppress small sizes and amplify large; dry boards do the reverse.
_WETNESS_MOD: dict = {
    "wet":      (0.50, 0.85, 1.20, 1.60),
    "semi_wet": (0.80, 1.00, 1.10, 1.15),
    "dry":      (1.40, 1.10, 0.85, 0.55),
}

# ── Preflop sizing tables ─────────────────────────────────────────────────────

# Open-raise BB multiples and weights (w3.0, w3.5, w4.0) per position.
# Steal positions favour the smaller end; early position skews larger.
_OPEN_SIZES = (3.0, 3.5, 4.0)
_OPEN_WEIGHTS: dict = {
    "BTN": (50, 35, 15),
    "CO":  (45, 35, 20),
    "HJ":  (40, 35, 25),
    "SB":  (35, 40, 25),
    "UTG": (25, 40, 35),
    "MP":  (30, 40, 30),
}
_OPEN_DEFAULT = (35, 40, 25)  # unknown position

# 3bet sizing: multiples of the original raise to call
_3BET_SIZES   = (3.0, 3.5, 4.0)
_3BET_WEIGHTS = (35, 40, 25)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _board_wetness(board_cards: list) -> str:
    """Classify the current board as 'wet', 'semi_wet', or 'dry'."""
    if len(board_cards) < 3:
        return "dry"
    score = 0
    ftex = board_texture(board_cards[:3])
    if   "monotone"  in ftex: score += 2
    elif "twotone"   in ftex: score += 1
    if   "connected" in ftex: score += 1
    if len(board_cards) >= 4 and turn_card_texture(board_cards)  in ("flush_complete", "straight_complete"):
        score += 1
    if len(board_cards) >= 5 and river_card_texture(board_cards) in ("flush_complete", "straight_complete"):
        score += 1
    if score >= 3: return "wet"
    if score >= 1: return "semi_wet"
    return "dry"


def _sample(sizes: tuple, weights) -> tuple:
    """Weighted sample. Returns (chosen_size, normalised_probs, roll)."""
    total = sum(weights)
    probs = tuple(w / total for w in weights)
    roll  = random.random()
    cum   = 0.0
    for size, prob in zip(sizes, probs):
        cum += prob
        if roll < cum:
            return size, probs, roll
    return sizes[-1], probs, roll


# ── Public interface ──────────────────────────────────────────────────────────

def pick_bet_fraction(
    hand_class:  str,
    board_cards: list,
    facing_bet:  bool,
) -> tuple:
    """
    Sample a pot fraction from {0.33, 0.66, 1.00, 1.50}.

    Returns (fraction, log_lines) where log_lines is a list[str] ready to
    extend onto the engine decision log.
    """
    table   = _RAISE_WEIGHTS if facing_bet else _BET_WEIGHTS
    base_w  = table.get(hand_class, table["air"])
    wetness = _board_wetness(board_cards)
    mod     = _WETNESS_MOD[wetness]

    adj_w             = tuple(b * m for b, m in zip(base_w, mod))
    frac, probs, roll = _sample(_SIZES, adj_w)

    action = "raising" if facing_bet else "betting"
    log_lines = [
        f"  Sizing  [{action}]  hand={hand_class}  board={wetness}",
        f"          33%={probs[0]:.0%}  66%={probs[1]:.0%}  "
        f"100%={probs[2]:.0%}  150%={probs[3]:.0%}",
        f"          roll={roll:.4f}  →  {int(frac * 100)}% pot",
    ]
    return frac, log_lines


def pick_preflop_size(
    position:     str,
    facing_raise: bool,
    to_call:      float,
    bb:           float,
) -> tuple:
    """
    Sample a preflop raise size.

    Returns (raise_to_amount, log_lines).
      facing_raise=False  → open raise, sized as BB multiple
      facing_raise=True   → 3bet, sized as multiple of to_call
    """
    if facing_raise:
        mult, probs, roll = _sample(_3BET_SIZES, _3BET_WEIGHTS)
        raise_to = round(to_call * mult, 2)
        log_lines = [
            f"  Sizing  [3bet]  "
            f"3×={probs[0]:.0%}  3.5×={probs[1]:.0%}  4×={probs[2]:.0%}",
            f"          roll={roll:.4f}  →  {mult}× raise  (to {raise_to:.2f})",
        ]
    else:
        weights  = _OPEN_WEIGHTS.get(position, _OPEN_DEFAULT)
        mult, probs, roll = _sample(_OPEN_SIZES, weights)
        raise_to = round(bb * mult, 2)
        log_lines = [
            f"  Sizing  [open]  pos={position}  "
            f"3×={probs[0]:.0%}  3.5×={probs[1]:.0%}  4×={probs[2]:.0%}",
            f"          roll={roll:.4f}  →  {mult}×BB  (to {raise_to:.2f})",
        ]
    return raise_to, log_lines
