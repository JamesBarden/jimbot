"""
Runtime query interface for the pre-computed GTO lookup table.

Usage:
  from solver_lookup import SolverLookup
  lut = SolverLookup()                              # loads once at startup
  freqs = lut.query(state, hole_cards, villain_tier)  # None if no match
"""

import os
import pickle
from typing import Optional, Tuple

from state import Card, GameState
from hand_classifier import classify, board_texture as classify_board_texture


# ── SPR bucketing ────────────────────────────────────────────────────────────

def _spr_bucket(my_stack: float, pot: float) -> str:
    if pot <= 0:
        return "high"
    spr = my_stack / pot
    if spr < 4:
        return "low"
    if spr < 11:
        return "medium"
    return "high"


# ── Villain tier normalisation ───────────────────────────────────────────────

# Map hand_tracker's 5 tiers to the 3 we solved for
_TIER_MAP = {
    "premium": "tight",
    "tight":   "tight",
    "medium":  "medium",
    "wide":    "wide",
    "random":  "wide",
}


# ── Lookup table ─────────────────────────────────────────────────────────────

class SolverLookup:
    """
    Loads the pre-built lookup table once and answers queries in O(1).

    lookup dict structure:
      (board_texture, spr_bucket, villain_tier)
        → {hand_class: (raise_freq, call_freq, fold_freq)}
    """

    def __init__(self, path: Optional[str] = None):
        if path is None:
            here = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(here, "solutions", "lookup.pkl")

        self._table: dict = {}
        self._loaded = False

        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    self._table = pickle.load(f)
                self._loaded = True
                print(f"[solver] Loaded GTO lookup: {len(self._table)} spots from {path}")
            except Exception as e:
                print(f"[solver] WARNING: failed to load lookup table: {e}")
        else:
            print(f"[solver] No lookup table found at {path} — using Monte Carlo fallback")
            print(f"         Run  python3 scripts/presolve.py && python3 scripts/build_lookup.py  to generate it.")

    @property
    def loaded(self) -> bool:
        return self._loaded

    def query(
        self,
        state: GameState,
        hole_cards: list[Card],
        villain_tier: str,
    ) -> Optional[Tuple[float, float, float]]:
        """
        Return (raise_freq, call_freq, fold_freq) from the lookup table.

        The lookup distinguishes two situations:
          facing_bet=False  — no bet facing us (can check), IP bets or checks
          facing_bet=True   — a bet is facing us, IP calls/raises/folds

        Returns None if:
          - the table is not loaded
          - the current spot has no matching entry
          - fewer than 3 board cards are showing
        """
        if not self._loaded or len(state.board_cards) < 3:
            return None

        tex        = classify_board_texture(state.board_cards)
        spr        = _spr_bucket(state.my_stack, state.pot)
        vtier      = _TIER_MAP.get(villain_tier, "medium")
        facing_bet = state.to_call > 0

        spot_key = (tex, spr, vtier, facing_bet)
        spot = self._table.get(spot_key)

        if spot is None:
            # Relax villain tier to find nearest match
            for fallback_tier in ("medium", "wide", "tight"):
                spot = self._table.get((tex, spr, fallback_tier, facing_bet))
                if spot is not None:
                    break

        if spot is None:
            return None

        hand_class = classify(hole_cards, state.board_cards)
        freqs = spot.get(hand_class)

        if freqs is None:
            freqs = _nearest_class_fallback(spot, hand_class)

        return freqs


# ── Fallback: nearest hand class by strength ────────────────────────────────

_CLASS_STRENGTH = [
    "air", "weak_draw", "draw", "combo_draw",
    "bottom_pair", "middle_pair", "top_pair_weak", "top_pair_top",
    "overpair", "two_pair", "set",
    "straight", "flush", "full_house", "monster",
]


def _nearest_class_fallback(
    spot: dict, hand_class: str
) -> Optional[Tuple[float, float, float]]:
    """Return the strategy for the closest available hand class."""
    if not spot:
        return None

    try:
        target_idx = _CLASS_STRENGTH.index(hand_class)
    except ValueError:
        return next(iter(spot.values()))

    available = [(abs(_CLASS_STRENGTH.index(k) - target_idx), k)
                 for k in spot if k in _CLASS_STRENGTH]
    if not available:
        return next(iter(spot.values()))

    _, best_key = min(available)
    return spot[best_key]
