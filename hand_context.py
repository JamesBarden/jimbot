"""
Per-hand state that persists across all four streets.

Each bot-played hand gets a HandContext that tracks:
  - our actions on each street (check/call/bet/raise/fold, with amounts)
  - villain actions inferred from state deltas (bet into us / checked to us)
  - who was the preflop aggressor (PFA)
  - whether we continuation-bet each street
  - the stack we started the hand with (for computing result delta)

The engine consumes this context to answer questions the raw GameState can't:
  - 'I c-bet the flop — is this a double-barrel spot on the turn?'
  - 'Did villain check-call the flop? I should consider a value bet/giveup.'
  - 'I've fired twice — is a third barrel in range?'

Lifecycle:
  1. main.py detects a new hand and calls HandContext(hand_id, ...)
  2. Before each engine.decide() call, main calls context.observe(state)
     so the context can infer villain actions from state deltas since the
     last observation.
  3. After engine.decide() returns, main calls context.record_hero(...).
  4. At hand end, context.finalize(end_stack) returns a summary dict for
     the hand log.
"""
from dataclasses import dataclass, field
from typing import Optional

from state import GameState


STREETS = ("preflop", "flop", "turn", "river")


@dataclass
class StreetRecord:
    hero_actions:   list[str] = field(default_factory=list)   # e.g. ['raise:0.60']
    villain_bet:    bool      = False    # did anyone bet/raise into us this street
    villain_checks: int       = 0        # number of check-observations we saw
    hero_bet:       bool      = False    # did we bet or raise
    hero_checked:   bool      = False
    hero_called:    bool      = False
    hero_folded:    bool      = False

    def summary(self) -> str:
        if self.hero_folded:  return "fold"
        if not self.hero_actions:
            return "none" if not self.villain_bet else "nofact"
        # Condense into a short string, e.g. 'bet/raise' or 'call' or 'check/call'
        parts = []
        for a in self.hero_actions:
            parts.append(a.split(":", 1)[0])
        return "/".join(parts)


@dataclass
class HandContext:
    hand_id:       str
    hole_cards:    tuple              # tuple of card strings for identity
    start_stack:   float
    bb:            float
    position:      str
    num_opponents: int
    villain_names: tuple = ()          # usernames of active opponents at hand start

    streets: dict[str, StreetRecord] = field(default_factory=lambda: {s: StreetRecord() for s in STREETS})

    # Running view of who is the "preflop aggressor" — updated each time
    # somebody raises preflop. 'hero', 'villain', or None.
    preflop_aggressor: Optional[str] = None

    # Reached the showdown street in the sense of seeing a river card
    reached_street: str = "preflop"

    # Last observed to_call per street (for delta inference on next observe)
    _last_to_call_per_street: dict[str, float] = field(default_factory=dict)
    # Count how many times we've been observed at a given street (to detect
    # a second visit on the same street, i.e. villain raised our bet)
    _observations_per_street: dict[str, int]   = field(default_factory=dict)

    # ── observation: infer villain activity from state changes ────────────────

    def observe(self, state: GameState):
        """Update context from a new GameState snapshot (before hero acts)."""
        s = state.phase
        if s in STREETS:
            self._observations_per_street[s] = self._observations_per_street.get(s, 0) + 1
            if s != self.reached_street:
                # Advance high-water mark when we see a later street
                if STREETS.index(s) > STREETS.index(self.reached_street):
                    self.reached_street = s

        rec = self.streets[s] if s in self.streets else None
        if rec is None:
            return

        # Villain activity inference:
        #   - First observation on a postflop street with to_call > 0 → villain bet into us
        #   - Same street, to_call re-appears after we bet → villain raised
        if s != "preflop":
            if state.to_call > 0:
                rec.villain_bet = True
            elif state.can_check:
                rec.villain_checks += 1

        # Preflop aggression tracking: if to_call > bb on our first look, someone raised
        if s == "preflop" and self.preflop_aggressor is None:
            if state.to_call > self.bb * 1.2:    # allow float slop on tiny-blind games
                self.preflop_aggressor = "villain"

        self._last_to_call_per_street[s] = state.to_call

    # ── hero action recording ────────────────────────────────────────────────

    def record_hero(self, street: str, action: str, amount: float = 0.0):
        """Append an action we took on the given street. Called after engine.decide."""
        if street not in self.streets:
            return
        rec = self.streets[street]
        rec.hero_actions.append(f"{action}:{amount:.2f}" if amount else action)

        if action == "raise":
            rec.hero_bet = True
            if street == "preflop":
                self.preflop_aggressor = "hero"
        elif action == "bet":   # engine uses 'raise' for open-bets, but keep for safety
            rec.hero_bet = True
            if street == "preflop":
                self.preflop_aggressor = "hero"
        elif action == "call":
            rec.hero_called = True
        elif action == "check":
            rec.hero_checked = True
        elif action == "fold":
            rec.hero_folded = True

    # ── queries the engine uses to condition its strategy ────────────────────

    def hero_was_pfa(self) -> bool:
        """True if hero was (or is) the preflop aggressor."""
        return self.preflop_aggressor == "hero"

    def hero_cbet(self, street: str) -> bool:
        """Did we bet/raise on a given postflop street?"""
        if street not in self.streets:
            return False
        return self.streets[street].hero_bet

    def is_double_barrel_spot(self, current_street: str) -> bool:
        """
        On the TURN: true if we cbet the flop (continuation-bet candidate).
        On the RIVER: true if we fired both flop and turn (triple-barrel spot).
        """
        if current_street == "turn":
            return self.hero_was_pfa() and self.hero_cbet("flop")
        if current_street == "river":
            return self.hero_was_pfa() and self.hero_cbet("flop") and self.hero_cbet("turn")
        return False

    def villain_check_called_last_street(self, current_street: str) -> bool:
        """
        Did villain check-call the immediately prior street?
        Useful on the turn/river to decide whether to barrel or give up.
        """
        prev_street_idx = STREETS.index(current_street) - 1
        if prev_street_idx < 1:  # only meaningful for turn onward
            return False
        prev = STREETS[prev_street_idx]
        rec = self.streets.get(prev)
        if rec is None:
            return False
        # Simple inference: hero bet that street and the street advanced
        # (meaning villain didn't fold) and the current street shows no bet yet
        return rec.hero_bet and not rec.villain_bet

    # ── finalisation ─────────────────────────────────────────────────────────

    def finalize(self, end_stack: float, final_board: list, final_pot: float,
                 villain_tier_end: str) -> dict:
        """
        Return a dict suitable for session_logger.log_hand().
        Called when the hand ends (new hand detected or fold).
        """
        return {
            "hand_id":       self.hand_id,
            "position":      self.position,
            "hole_cards":    " ".join(self.hole_cards),
            "final_board":   " ".join(str(c) for c in final_board),
            "preflop_action": self.streets["preflop"].summary(),
            "flop_action":    self.streets["flop"].summary(),
            "turn_action":    self.streets["turn"].summary(),
            "river_action":   self.streets["river"].summary(),
            "pot_final":     round(final_pot, 2),
            "stack_start":   round(self.start_stack, 2),
            "stack_end":     round(end_stack, 2),
            "stack_delta":   round(end_stack - self.start_stack, 2),
            "bb":            round(self.bb, 2),
            "reached_street": self.reached_street,
            "num_opponents": self.num_opponents,
            "villain_tier_end": villain_tier_end,
            "villain_names":  ",".join(self.villain_names),
        }
