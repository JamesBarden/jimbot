"""
Combo-level range tracking for both players.

Replaces the 5-tier opponent abstraction (premium/tight/medium/wide/random)
with explicit weighted combo distributions. Each player has a Range — a
dict mapping each frozenset[Card] (a 2-card combo) to a non-negative weight
representing the relative likelihood that the player holds that combo given
all observed actions in the current hand.

Workflow per hand:
  1. Both ranges initialized to all 1326 combos at weight 1.0.
  2. Combos containing known cards (our hole cards, board) are filtered out
     from the opponent's range so we don't sample impossible holdings.
  3. After each observed action, narrow_*() multiplies each combo's weight
     by the per-combo frequency that combo would take that action under the
     strategy table for that spot. Combos that fold get pruned (weight → 0).
  4. Equity queries sample from the current weighted distribution.

For our own range, the same machinery applies — we track what range we are
*representing* by our actions, not what we actually hold. This is what lets
the engine ask "is my range stronger than villain's range here?" to find
bluff spots independent of our specific hand. (Actual holding still drives
big calls, value bets, and pot-commit decisions where we don't want to bluff
into our own range.)
"""

from __future__ import annotations
import random
from itertools import combinations
from typing import Callable, Iterable, Optional

from browser.state import Card, GameState
from decision.preflop_ranges import lookup as _preflop_lookup
from decision.hand_classifier import (
    classify as _classify,
    board_texture as _flop_texture_fn,
    turn_card_texture as _turn_texture_fn,
    river_card_texture as _river_texture_fn,
)
from decision import turn_heuristic, river_heuristic
from decision.solver_lookup import SolverLookup, _TIER_MAP, _spr_bucket

_RANKS = "23456789TJQKA"
_SUITS = "shdc"


def _build_deck() -> list[Card]:
    return [Card(rank=r, suit=s) for r in _RANKS for s in _SUITS]


_DECK: list[Card] = _build_deck()
ALL_COMBOS: list[frozenset[Card]] = [frozenset(c) for c in combinations(_DECK, 2)]


# ── Range data structure ─────────────────────────────────────────────────────

class Range:
    """
    Weighted combo distribution.

    weights[combo] is a non-negative real number. The distribution is
    proportional to these weights — they need not sum to 1. Combos with
    weight 0 are pruned to keep narrowing fast.
    """

    __slots__ = ("weights",)

    def __init__(self, weights: Optional[dict[frozenset[Card], float]] = None):
        if weights is None:
            self.weights = {c: 1.0 for c in ALL_COMBOS}
        else:
            self.weights = {c: w for c, w in weights.items() if w > 0}

    @classmethod
    def uniform(cls) -> "Range":
        """All 1326 combos, weight 1.0."""
        return cls()

    def filter_known(self, known_cards: Iterable[Card]) -> "Range":
        """Return a new Range with combos that conflict with known_cards removed."""
        known_set = set(known_cards)
        if not known_set:
            return Range(self.weights)
        return Range({
            c: w for c, w in self.weights.items()
            if not (c & known_set)
        })

    def apply(self, freq_fn: Callable[[frozenset[Card]], float]) -> "Range":
        """
        Multiply each combo's weight by a per-combo frequency (in [0, 1]).

        freq_fn(combo) should return the probability that a player holding
        `combo` would have taken the action being filtered on.
        """
        return Range({
            c: w * freq_fn(c)
            for c, w in self.weights.items()
        })

    def normalized(self) -> "Range":
        total = self.total()
        if total <= 0:
            return Range({})
        inv = 1.0 / total
        return Range({c: w * inv for c, w in self.weights.items()})

    def total(self) -> float:
        return sum(self.weights.values())

    def num_combos(self) -> int:
        """Number of combos with non-zero weight."""
        return len(self.weights)

    def sample(self, rng: Optional[random.Random] = None) -> Optional[frozenset[Card]]:
        """Weighted random sample of one combo. Returns None if range is empty."""
        rng = rng or random
        total = self.total()
        if total <= 0:
            return None
        target = rng.random() * total
        cum = 0.0
        for c, w in self.weights.items():
            cum += w
            if cum >= target:
                return c
        # Floating-point edge: fall through to last combo.
        return next(reversed(self.weights))

    def top_n(self, n: int = 10) -> list[tuple[frozenset[Card], float]]:
        """Combos sorted by weight, descending. Useful for debugging."""
        return sorted(self.weights.items(), key=lambda x: -x[1])[:n]

    def to_tier(self) -> str:
        """
        Backward-compat: derive a coarse 5-tier label from current range size.

        Calibrated against typical narrowed ranges:
          - premium (≤50 combos):  3-bet/4-bet ranges (TT+, AKs/AKo)
          - tight   (≤150 combos): tight UTG opens (88+, AJ+, KQ)
          - medium  (≤400 combos): standard CO/HJ opens
          - wide    (≤900 combos): BTN opens, BB defends
          - random  (>900):        unfiltered or near-unfiltered
        """
        n = self.num_combos()
        if n <= 50:   return "premium"
        if n <= 150:  return "tight"
        if n <= 400:  return "medium"
        if n <= 900:  return "wide"
        return "random"

    def __repr__(self) -> str:
        return f"Range(num_combos={self.num_combos()}, total={self.total():.2f}, tier={self.to_tier()})"


# ── Preflop narrowing primitives ─────────────────────────────────────────────

def _preflop_freq(combo: frozenset[Card], position: str, facing_raise: bool,
                   num_players: int, action: str) -> float:
    """P(action | combo) at this preflop spot. action ∈ {'raise', 'call', 'fold'}."""
    cards = list(combo)
    r, c, f = _preflop_lookup(cards, position, facing_raise, num_players=num_players)
    if action == "raise": return r
    if action == "call":  return c
    if action == "fold":  return f
    return 0.0


def narrow_on_preflop_action(
    range_: Range,
    position: str,
    facing_raise: bool,
    num_players: int,
    action: str,
) -> Range:
    """Filter a player's range to combos consistent with the preflop action they took."""
    return range_.apply(
        lambda combo: _preflop_freq(combo, position, facing_raise, num_players, action)
    )


# ── Postflop narrowing primitives ────────────────────────────────────────────
#
# Each `narrow_on_*_action()` mirrors the strategy table the engine consulted
# when actually making the decision. For a given board + spot context we look
# up each combo's hand class and read off the frequency it would take the
# observed action. Combos take the action proportionally to that frequency.
#
# Keeping these primitives aligned with the engine's tables matters: if the
# engine c-bets `air` at 32% on this flop, the inferred range after a c-bet
# should weight `air` combos at 32% — anything else creates a self-inconsistent
# model where our bot believes things about villain (or itself) that contradict
# how it would actually play that combo.

# Lazily-loaded shared SolverLookup instance for per-combo flop frequencies.
_solver_lookup: Optional[SolverLookup] = None


def _get_solver_lookup() -> SolverLookup:
    global _solver_lookup
    if _solver_lookup is None:
        _solver_lookup = SolverLookup()
    return _solver_lookup


def _action_to_index(action: str) -> int:
    """Map a 5-way action label to the 3-tuple index returned by tables."""
    return {"raise": 0, "bet": 0,
            "call":  1, "check": 1,
            "fold":  2}.get(action, 1)


def narrow_on_flop_action(
    range_: Range,
    state: GameState,
    villain_tier: str,
    action: str,
) -> Range:
    """
    Narrow on a flop action by per-combo solver-lookup classification.

    Mirrors `decision.solver_lookup.SolverLookup.query()`: same texture / SPR
    bucket / villain-tier mapping, just applied to every combo via its
    hand class on the flop instead of only the hero's specific hand.

    Falls back to a no-op (returns the range unchanged) when the solver
    lookup table isn't available — the caller should then narrow with the
    turn/river heuristic primitives if they were used as fallback.
    """
    if len(state.board_cards) < 3:
        return range_

    lut = _get_solver_lookup()
    if not lut.loaded:
        return range_

    tex = _flop_texture_fn(state.board_cards[:3])
    spr_b = _spr_bucket(state.my_stack, state.pot)
    vtier = _TIER_MAP.get(villain_tier, "medium")
    facing_bet = state.to_call > 0

    spot = lut._table.get((tex, spr_b, vtier, facing_bet))
    if spot is None:
        for fallback in ("medium", "wide", "tight"):
            spot = lut._table.get((tex, spr_b, fallback, facing_bet))
            if spot is not None:
                break
    if spot is None:
        return range_

    idx = _action_to_index(action)
    board = list(state.board_cards)

    def freq(combo: frozenset[Card]) -> float:
        cards = list(combo)
        hclass = _classify(cards, board)
        freqs = spot.get(hclass)
        if freqs is None:
            return 0.0
        return freqs[idx]

    return range_.apply(freq)


def narrow_on_turn_action(
    range_: Range,
    state: GameState,
    position: str,
    villain_tier: str,
    spr: float,
    bet_fraction: float,
    facing_bet: bool,
    action: str,
    context=None,
) -> Range:
    """Per-combo turn-heuristic narrowing on the action that was taken."""
    if len(state.board_cards) < 4:
        return range_

    idx = _action_to_index(action)
    board = list(state.board_cards)

    def freq(combo: frozenset[Card]) -> float:
        cards = list(combo)
        freqs = turn_heuristic.query(
            hole_cards=cards,
            board_cards=board,
            position=position,
            villain_tier=villain_tier,
            facing_bet=facing_bet,
            spr=spr,
            bet_fraction=bet_fraction,
            context=context,
        )
        return freqs[idx]

    return range_.apply(freq)


def narrow_on_river_action(
    range_: Range,
    state: GameState,
    position: str,
    villain_tier: str,
    spr: float,
    bet_fraction: float,
    facing_bet: bool,
    action: str,
    context=None,
) -> Range:
    """Per-combo river-heuristic narrowing on the action that was taken."""
    if len(state.board_cards) < 5:
        return range_

    idx = _action_to_index(action)
    board = list(state.board_cards)

    def freq(combo: frozenset[Card]) -> float:
        cards = list(combo)
        freqs = river_heuristic.query(
            hole_cards=cards,
            board_cards=board,
            position=position,
            villain_tier=villain_tier,
            facing_bet=facing_bet,
            spr=spr,
            bet_fraction=bet_fraction,
            context=context,
        )
        return freqs[idx]

    return range_.apply(freq)


# ── Equity (range-aware MC) ──────────────────────────────────────────────────

def hand_vs_range_equity(
    hero_cards: list[Card],
    villain_range: Range,
    board_cards: list[Card],
    num_sims: int = 1500,
    rng: Optional[random.Random] = None,
) -> float:
    """
    Estimate hero's win probability vs villain's weighted range.

    Drop-in replacement for engine.monte_carlo_equity that samples from a
    Range instead of a tier. Combos in villain_range that conflict with
    hero_cards or board_cards are excluded automatically.
    """
    from treys import Card as TreysCard, Evaluator

    rng = rng or random
    evaluator = Evaluator()

    def to_treys(c: Card) -> int:
        return TreysCard.new(c.to_treys())

    hero_treys = [to_treys(c) for c in hero_cards]
    board_treys = [to_treys(c) for c in board_cards]
    known_cards = set(hero_cards) | set(board_cards)

    sampleable = villain_range.filter_known(known_cards)
    if sampleable.total() <= 0:
        return 0.5

    full_deck = [c for c in _DECK if c not in known_cards]
    cards_needed = 5 - len(board_cards)

    wins = 0.0
    valid = 0

    for _ in range(num_sims):
        villain_combo = sampleable.sample(rng)
        if villain_combo is None:
            continue
        villain_cards = list(villain_combo)
        villain_treys = [to_treys(c) for c in villain_cards]

        deck = [c for c in full_deck if c not in villain_combo]
        if cards_needed > 0:
            if len(deck) < cards_needed:
                continue
            runout_treys = [to_treys(c) for c in rng.sample(deck, cards_needed)]
        else:
            runout_treys = []

        full_board = board_treys + runout_treys
        # treys: lower score is a stronger hand
        hero_score = evaluator.evaluate(full_board, hero_treys)
        villain_score = evaluator.evaluate(full_board, villain_treys)

        if hero_score < villain_score:
            wins += 1.0
        elif hero_score == villain_score:
            wins += 0.5
        valid += 1

    if valid == 0:
        return 0.5
    return wins / valid


def range_vs_range_equity(
    hero_range: Range,
    villain_range: Range,
    board_cards: list[Card],
    num_sims: int = 1500,
    rng: Optional[random.Random] = None,
) -> float:
    """
    Average win rate of hero's range vs villain's range, sampled from the joint
    distribution. Used to detect range-advantage spots for bluff modulation:
    when our range outperforms villain's on a given board, we can c-bet wider.
    """
    from treys import Card as TreysCard, Evaluator

    rng = rng or random
    evaluator = Evaluator()

    def to_treys(c: Card) -> int:
        return TreysCard.new(c.to_treys())

    board_treys = [to_treys(c) for c in board_cards]
    board_set = set(board_cards)
    cards_needed = 5 - len(board_cards)

    hero_filtered = hero_range.filter_known(board_set)
    if hero_filtered.total() <= 0 or villain_range.total() <= 0:
        return 0.5

    full_deck = [c for c in _DECK if c not in board_set]

    wins = 0.0
    valid = 0

    for _ in range(num_sims):
        hero_combo = hero_filtered.sample(rng)
        if hero_combo is None:
            continue
        # Filter villain to combos that don't share cards with hero or board.
        v_filtered = villain_range.filter_known(hero_combo | board_set)
        villain_combo = v_filtered.sample(rng)
        if villain_combo is None:
            continue

        used = hero_combo | villain_combo | board_set
        deck = [c for c in full_deck if c not in used]
        if cards_needed > 0:
            if len(deck) < cards_needed:
                continue
            runout_treys = [to_treys(c) for c in rng.sample(deck, cards_needed)]
        else:
            runout_treys = []

        hero_treys = [to_treys(c) for c in hero_combo]
        villain_treys = [to_treys(c) for c in villain_combo]
        full_board = board_treys + runout_treys

        hero_score = evaluator.evaluate(full_board, hero_treys)
        villain_score = evaluator.evaluate(full_board, villain_treys)

        if hero_score < villain_score:
            wins += 1.0
        elif hero_score == villain_score:
            wins += 0.5
        valid += 1

    if valid == 0:
        return 0.5
    return wins / valid
