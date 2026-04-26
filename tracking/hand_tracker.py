"""
Tracks opponent actions across a hand to infer their likely range.

Since we read state snapshots rather than an action stream, we infer actions
from state transitions:
  - New hole cards → new hand, reset everything
  - to_call > 0 on a street we haven't seen a bet on → opponent bet/raised
  - Street changes → record what happened preflop for the rest of the hand

Range inference rules (heads-up):
  Preflop
    to_call >= 6×BB  → opponent 3-bet or shoved      → PREMIUM
    to_call >= 3×BB  → opponent made a large raise    → TIGHT
    to_call >= 1×BB  → opponent made a standard raise → MEDIUM
    to_call == 0     → opponent limped or we raised   → WIDE
  Postflop (applied on top of preflop range)
    opponent bet this street                          → tighten one tier
    opponent checked (to_call stayed 0)               → no change
"""

from typing import Optional
from browser.state import GameState

# Ordered from tightest to loosest
_TIERS = ["premium", "tight", "medium", "wide", "random"]


def _tighten(tier: str) -> str:
    idx = _TIERS.index(tier)
    return _TIERS[max(0, idx - 1)]


class HandTracker:
    def __init__(self):
        self._hole_key: Optional[tuple] = None  # detect hand changes
        self._preflop_tier = "random"
        self._current_tier = "random"
        self._streets_seen: set[str] = set()
        self._bet_seen_this_street = False
        self._last_phase: str | None = None

    def _reset(self, state: GameState):
        self._hole_key = self._key(state)
        self._preflop_tier = "random"
        self._current_tier = "random"
        self._streets_seen = set()
        self._bet_seen_this_street = False
        self._last_phase = None

    @staticmethod
    def _key(state: GameState) -> tuple:
        return tuple(str(c) for c in state.hole_cards)

    def update(self, state: GameState) -> str:
        """
        Consume the latest GameState and return the current range tier name.
        Call this once per decision, before passing the tier to the engine.
        """
        # Detect new hand
        if self._key(state) != self._hole_key:
            self._reset(state)

        # Street transition
        if state.phase != self._last_phase:
            self._bet_seen_this_street = False
            self._last_phase = state.phase

        # Preflop: calibrate from to_call relative to big blind
        if state.phase == "preflop" and "preflop" not in self._streets_seen:
            bb = state.big_blind if state.big_blind > 0 else 0.01
            ratio = state.to_call / bb
            if ratio >= 6:
                self._preflop_tier = "premium"
            elif ratio >= 3:
                self._preflop_tier = "tight"
            elif ratio >= 1:
                self._preflop_tier = "medium"
            else:
                self._preflop_tier = "wide"
            self._current_tier = self._preflop_tier
            self._streets_seen.add("preflop")

        # Postflop: tighten once per street where opponent bet into us
        if state.phase != "preflop" and state.to_call > 0 and not self._bet_seen_this_street:
            self._current_tier = _tighten(self._current_tier)
            self._bet_seen_this_street = True
            self._streets_seen.add(state.phase)

        return self._current_tier

    @property
    def preflop_tier(self) -> str:
        return self._preflop_tier

    @property
    def current_tier(self) -> str:
        return self._current_tier
