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

from browser.state import Card, GameState
from tracking.range_tracker import (
    Range,
    narrow_on_preflop_action,
    narrow_on_flop_action,
    narrow_on_turn_action,
    narrow_on_river_action,
)


STREETS = ("preflop", "flop", "turn", "river")


@dataclass
class StreetRecord:
    hero_actions:    list[str] = field(default_factory=list)   # e.g. ['raise:0.60']
    villain_actions: list[str] = field(default_factory=list)   # inferred, e.g. ['raise', 'call']
    villain_bet:     bool      = False    # did anyone bet/raise into us this street
    villain_checks:  int       = 0        # number of check-observations we saw
    hero_bet:        bool      = False    # did we bet or raise
    hero_checked:    bool      = False
    hero_called:     bool      = False
    hero_folded:     bool      = False
    # Snapshot of the board on this street's first observation. Used at
    # street-transition time to reconstruct the spot for per-combo narrowing.
    board_at_first_obs: tuple = ()
    # to_call / pot at first observation — lets us recover the bet_fraction
    # villain faced (or the size of villain's bet into us) without retro-
    # observing partial-street state.
    to_call_at_first_obs: float = 0.0
    pot_at_first_obs:     float = 0.0

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

    # Snapshot of the active villain set at the FIRST observation of each street.
    # Used to do multiway-safe attribution: anyone in flop set but not in turn set
    # folded the flop, anyone in turn set but not in river set folded the turn, etc.
    villains_per_street: dict[str, frozenset] = field(default_factory=dict)

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
    # my_stack snapshot at the first observation of each street.  Lets us
    # reconstruct hero's per-street chip contribution as
    # (stack_at_street_start - current_stack), used by the engine to estimate
    # the true on-table pot when the scraper's pot reading is stuck on
    # closed-streets-only.
    stack_at_street_start: dict = field(default_factory=dict)

    # ── Range tracking ────────────────────────────────────────────────────────
    # Combo-level distributions for both players, narrowed action-by-action
    # across all four streets. Initialised lazily by init_ranges() once we
    # have hero's hole cards. None means range tracking is disabled for this
    # hand (engine falls back to tier-based equity).
    hero_range:    Optional[Range] = None
    villain_range: Optional[Range] = None
    # We narrow villain's range on street transitions, not per-poll, so the
    # narrowing fires at most once per (street, player) pair.
    _villain_narrowed_streets: set = field(default_factory=set)
    # Track our position relative to villain so we can use the right table
    # when narrowing villain's preflop action.
    villain_position: str = "unknown"

    # Phase 4: chained preflop narrowing. Detect each villain re-raise (3-bet,
    # 5-bet, etc.) by comparing state.to_call to the snapshot taken after our
    # last action. Each detected re-raise narrows villain on VS_RAISE('raise').
    # Without this, narrowing fires only once at the first observed open and
    # multi-bet pots end up with stale-wide villain ranges, inflating
    # range-vs-range equity and triggering bad bluff-raises (cf. 2026-04-27
    # QcAc hand: 4-bet pot, modeled villain stayed "wide" because the 3-bet
    # and call-of-4-bet were never narrowed).
    _preflop_to_call_snapshot: float = 0.0
    _preflop_observed:         bool  = False
    villain_preflop_raises:    int   = 0   # count of re-raises beyond the open

    # ── range initialisation ──────────────────────────────────────────────────

    def init_ranges(self, hero_cards: list):
        """
        Initialise hero_range as uniform-over-1326 and villain_range as
        uniform-minus-our-known-cards. Call this exactly once at hand start,
        right after constructing HandContext, while hero_cards are visible.
        """
        self.hero_range = Range.uniform()
        self.villain_range = Range.uniform().filter_known(hero_cards)

    # ── observation: infer villain activity from state changes ────────────────

    def observe(self, state: GameState):
        """Update context from a new GameState snapshot (before hero acts)."""
        s = state.phase
        prev_street = self.reached_street if self.reached_street in STREETS else None
        if s in STREETS:
            self._observations_per_street[s] = self._observations_per_street.get(s, 0) + 1
            if s != self.reached_street:
                # Advance high-water mark when we see a later street.
                # Before advancing, narrow villain's range based on what they
                # did to close the prior street (call our raise / check back /
                # bet into us / etc).
                if STREETS.index(s) > STREETS.index(self.reached_street):
                    if self.villain_range is not None and prev_street is not None:
                        self._narrow_villain_on_street_close(prev_street)
                    self.reached_street = s
            # First observation of this street → snapshot who's still in the
            # hand AND our current stack (used to compute per-street chip
            # contribution later).
            if s not in self.villains_per_street:
                self.villains_per_street[s] = frozenset(state.villain_names)
            if s not in self.stack_at_street_start:
                self.stack_at_street_start[s] = state.my_stack

        rec = self.streets[s] if s in self.streets else None
        if rec is None:
            return

        # On first observation of this street, snapshot board + bet sizing for
        # later range narrowing. We also filter both ranges against the new
        # board cards so impossible combos (containing a board card) drop out.
        if not rec.board_at_first_obs and state.board_cards:
            rec.board_at_first_obs    = tuple(state.board_cards)
            rec.to_call_at_first_obs  = state.to_call
            rec.pot_at_first_obs      = state.pot
            if self.hero_range is not None:
                self.hero_range = self.hero_range.filter_known(state.board_cards)
            if self.villain_range is not None:
                self.villain_range = self.villain_range.filter_known(state.board_cards)

        # Villain activity inference:
        #   - First observation on a postflop street with to_call > 0 → villain bet into us
        #   - Same street, to_call re-appears after we bet → villain raised
        if s != "preflop":
            if state.to_call > 0:
                rec.villain_bet = True
            elif state.can_check:
                rec.villain_checks += 1

        # Phase 4: chained preflop villain narrowing.
        #
        # Two detection paths:
        #   (a) First preflop observation already shows to_call > BB → villain
        #       opened before our turn. Narrow on RFI('raise').
        #   (b) On a subsequent preflop observation, state.to_call has gone up
        #       since our last action's snapshot → villain re-raised since.
        #       Narrow on VS_RAISE('raise'). Repeats for every 3-bet/4-bet/etc.
        #
        # `_preflop_to_call_snapshot` is reset to 0 in record_hero() right
        # after each hero action so the next villain action is always
        # detectable as state.to_call > snapshot.
        if s == "preflop":
            if not self._preflop_observed:
                self._preflop_observed = True
                self._preflop_to_call_snapshot = state.to_call
                if state.to_call > self.bb * 1.2:
                    self.preflop_aggressor = "villain"
                    if self.villain_range is not None:
                        self._narrow_villain_preflop_open(state)
            else:
                # Subsequent preflop observation
                if state.to_call > self._preflop_to_call_snapshot + 1e-6:
                    # to_call increased since our last action → villain re-raised
                    self.preflop_aggressor = "villain"
                    self.villain_preflop_raises += 1
                    if self.villain_range is not None:
                        self._narrow_villain_preflop_reraise(state)
                    self._preflop_to_call_snapshot = state.to_call

        self._last_to_call_per_street[s] = state.to_call

    # ── hero action recording ────────────────────────────────────────────────

    def record_hero(self, street: str, action: str, amount: float = 0.0,
                    state: Optional[GameState] = None,
                    villain_tier: str = "medium",
                    spr: float = 5.0):
        """
        Append an action we took on the given street. Called after engine.decide.

        If `state` is provided AND hero_range is initialised, also narrows
        hero_range on this action by per-combo lookup against the strategy
        table the engine consulted for this spot.
        """
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

        # Phase 4: after every hero preflop action, reset the to_call snapshot
        # so the next preflop observation can detect a fresh villain raise via
        # state.to_call > snapshot. We just matched/raised so our outstanding
        # to_call is 0 from villain's next perspective.
        if street == "preflop":
            self._preflop_to_call_snapshot = 0.0

        # Narrow hero_range on the action we just took, if range tracking is on.
        if self.hero_range is not None and state is not None:
            self.hero_range = self._narrow_range(
                self.hero_range,
                street     = street,
                action     = action,
                state      = state,
                position   = state.position,
                villain_tier = villain_tier,
                spr        = spr,
                facing_bet = (state.to_call > 0),
                bet_fraction = (state.to_call / max(state.pot, 1e-9)) if state.to_call > 0 else 0.0,
                num_players = state.num_opponents + 1,
                facing_raise = (state.to_call > self.bb * 1.2),
            )

    # ── range narrowing helpers ──────────────────────────────────────────────

    def _narrow_range(self, range_: Range, *, street: str, action: str,
                      state: GameState, position: str, villain_tier: str,
                      spr: float, facing_bet: bool, bet_fraction: float,
                      num_players: int, facing_raise: bool) -> Range:
        """
        Apply the appropriate per-street narrowing primitive. Returns the
        narrowed range; falls back to the input range on any error so a
        narrowing bug can't crash the live decision loop.
        """
        try:
            if street == "preflop":
                return narrow_on_preflop_action(
                    range_, position, facing_raise, num_players, action,
                )
            if street == "flop":
                return narrow_on_flop_action(range_, state, villain_tier, action)
            if street == "turn":
                return narrow_on_turn_action(
                    range_, state, position, villain_tier, spr,
                    bet_fraction, facing_bet, action, context=self,
                )
            if street == "river":
                return narrow_on_river_action(
                    range_, state, position, villain_tier, spr,
                    bet_fraction, facing_bet, action, context=self,
                )
        except Exception:
            return range_
        return range_

    def _hu_villain_position(self, state: GameState) -> str:
        """
        Best-effort villain-position label for HU narrowing.

        In HU the dealer button is the small blind. The scraper labels hero's
        position as either 'BTN' (when hero has the button = hero is SB) or
        'BB' (when hero is the big blind). Either way, villain is the OTHER
        blind. Mapping:
          hero='BTN' or 'SB'  →  villain is BB
          hero='BB'           →  villain is SB
          anything else / multiway → 'unknown' (caller can fall back)
        """
        if state.num_opponents != 1:
            return "unknown"
        if state.position in ("BTN", "SB"):
            return "BB"
        if state.position == "BB":
            return "SB"
        return "unknown"

    def _narrow_villain_preflop_open(self, state: GameState):
        """Narrow villain on a preflop open/raise inferred from to_call > BB."""
        if self.villain_range is None:
            return
        v_pos = self._hu_villain_position(state)
        self.villain_position = v_pos
        try:
            self.villain_range = narrow_on_preflop_action(
                self.villain_range,
                position     = v_pos,
                facing_raise = False,   # villain is the one opening
                num_players  = state.num_opponents + 1,
                action       = "raise",
            )
        except Exception:
            pass

    def _narrow_villain_preflop_reraise(self, state: GameState):
        """
        Narrow villain on a preflop re-raise (3-bet, 5-bet, etc.) detected via
        state.to_call increasing since our last action's snapshot.

        Uses the VS_RAISE table's 'raise' frequencies — i.e. each combo's
        probability of 3-betting a single open from this position. Applied
        repeatedly across multi-bet pots, this approximates the actual
        narrowing posterior (P(open) × P(3bet | open) × ... ).
        """
        if self.villain_range is None:
            return
        v_pos = self.villain_position or self._hu_villain_position(state)
        try:
            self.villain_range = narrow_on_preflop_action(
                self.villain_range,
                position     = v_pos,
                facing_raise = True,    # villain is re-raising over a prior raise
                num_players  = state.num_opponents + 1,
                action       = "raise",
            )
        except Exception:
            pass

    def _narrow_villain_on_street_close(self, prev_street: str):
        """
        Best-effort inference of villain's closing action on prev_street, used
        to narrow villain_range as we transition into the next street.

        Rules (HU-centric, multiway approximated as same):
          - If hero was the last aggressor and the street advanced → villain CALLED.
          - If neither bet on a postflop street → both checked → villain CHECKED.
          - If villain bet and hero called → villain BET (already narrowed
            implicitly by to_call detection, but we narrow on bet here).
          - Anything else → no-op (we don't know enough to narrow safely).
        """
        if self.villain_range is None:
            return
        if prev_street in self._villain_narrowed_streets:
            return

        rec = self.streets.get(prev_street)
        if rec is None:
            return

        # Reconstruct enough state to call the per-street primitive. We use
        # the snapshot taken on first observation of prev_street.
        board = list(rec.board_at_first_obs)
        snap_state = GameState(
            hole_cards=[],
            board_cards=board,
            pot=rec.pot_at_first_obs,
            to_call=rec.to_call_at_first_obs,
            my_stack=self.stack_at_street_start.get(prev_street, 0.0),
            big_blind=self.bb,
            num_opponents=self.num_opponents,
            phase=prev_street,
            is_my_turn=False,
            can_check=(rec.to_call_at_first_obs == 0),
            position=self.position,
        )

        if self.hero_was_last_aggressor(prev_street):
            inferred_action = "call"
            facing_bet = True
        elif rec.villain_bet and rec.hero_called:
            inferred_action = "bet"
            facing_bet = False   # narrowing villain's "lead bet" — they weren't facing one
        elif (not rec.hero_bet) and (not rec.villain_bet):
            inferred_action = "check"
            facing_bet = False
        else:
            return   # ambiguous — bail out silently

        rec.villain_actions.append(inferred_action)

        # Best-effort villain position for postflop heuristic narrowing.
        v_pos = self.villain_position
        if v_pos == "unknown" and self.num_opponents == 1:
            # Same HU mapping as _hu_villain_position(); using self.position
            # since we don't carry a fresh state into this code path.
            if self.position in ("BTN", "SB"):
                v_pos = "BB"
            elif self.position == "BB":
                v_pos = "SB"

        try:
            spr = (snap_state.my_stack / snap_state.pot) if snap_state.pot > 0 else 5.0
            bet_frac = (snap_state.to_call / snap_state.pot) if snap_state.pot > 0 and snap_state.to_call > 0 else 0.0
            tier = self.villain_range.to_tier()
            if prev_street == "flop":
                self.villain_range = narrow_on_flop_action(
                    self.villain_range, snap_state, tier, inferred_action,
                )
            elif prev_street == "turn":
                self.villain_range = narrow_on_turn_action(
                    self.villain_range, snap_state, v_pos, tier, spr,
                    bet_frac, facing_bet, inferred_action, context=self,
                )
            elif prev_street == "river":
                self.villain_range = narrow_on_river_action(
                    self.villain_range, snap_state, v_pos, tier, spr,
                    bet_frac, facing_bet, inferred_action, context=self,
                )
        except Exception:
            pass
        self._villain_narrowed_streets.add(prev_street)

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

    def hero_was_last_aggressor(self, street: str) -> bool:
        """True if hero's last recorded action on the given street was bet/raise."""
        rec = self.streets.get(street)
        if rec is None or not rec.hero_actions:
            return False
        last = rec.hero_actions[-1].split(":", 1)[0]
        return last in ("raise", "bet")

    def hero_round_contribution(self, street: str, current_stack: float) -> float:
        """
        Chips hero has put in the pot on the given street, derived from
        observation snapshots: stack_at_street_start - current_stack.

        Used by the engine to reconstruct the on-table pot when the scraper's
        pot reading misses current-street action (a recurring pokernow DOM
        quirk where the displayed pot stays stuck on closed-streets total
        during a live betting round).

        Returns 0 if no snapshot was taken (street never observed) or if
        stack hasn't decreased.
        """
        start = self.stack_at_street_start.get(street)
        if start is None:
            return 0.0
        return max(0.0, start - current_stack)

    def villain_check_called_last_street(self, current_street: str) -> bool:
        """
        Did villain end the prior street by calling our aggression?
        True iff hero's last action on that street was a bet or raise.

        This catches both the simple case (hero bet, villain called) AND the
        re-raise case (villain bet, hero raised, villain called) — anywhere
        we were the *last* aggressor and the street advanced.
        """
        prev_street_idx = STREETS.index(current_street) - 1
        if prev_street_idx < 1:  # only meaningful for turn onward
            return False
        return self.hero_was_last_aggressor(STREETS[prev_street_idx])

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
