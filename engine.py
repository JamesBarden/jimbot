"""
Poker decision engine.

Decision priority:
  1. GTO lookup (solver_lookup.py) — uses pre-computed TexasSolver strategies
     indexed by board texture, SPR, villain tier, and hand class.
     Only available for postflop after running scripts/presolve.py and
     scripts/build_lookup.py.

  2. Monte Carlo fallback — range-weighted equity simulation with mixed-strategy
     randomisation (used preflop always, and postflop when lookup misses).

Preflop:  Chen formula scores hole cards on a 0-1 scale.
Postflop: Range-weighted Monte Carlo equity — instead of dealing opponents
          random hands, we sample from an inferred range (e.g. "tight" if
          they made a large raise preflop).

Decision model: mixed strategies (GTO-style).
  Action frequencies are computed from equity distance from each threshold
  with a blend zone (BLEND_WIDTH) around each boundary, then a random roll
  selects the action. This prevents deterministic exploitability.

  SPR (stack-to-pot ratio) tightens the raise threshold in deep spots.

Tuning thresholds: see the THRESHOLDS section below.
"""
import random
from treys import Card as TreysCard, Evaluator

from browser.state import Card, GameState
from decision.ranges import get_combos
from decision.solver_lookup import SolverLookup
from decision.hand_classifier import classify, board_texture as flop_texture, turn_card_texture, river_card_texture
from decision import preflop_ranges, turn_heuristic, river_heuristic
from decision.bet_sizing import pick_bet_fraction, pick_preflop_size
from tracking.range_tracker import hand_vs_range_equity, range_vs_range_equity

_lut = SolverLookup()

_evaluator = Evaluator()


# ---------------------------------------------------------------------------
# THRESHOLDS — edit these to change how aggressive the bot plays
# ---------------------------------------------------------------------------

# Postflop (Monte Carlo equity, 0-1 scale)
POSTFLOP_RAISE_THRESHOLD = 0.60  # raise if equity >= this
POSTFLOP_CALL_EDGE       = 0.05  # call if equity > pot_odds + this margin

# Monte Carlo simulation count — lower = faster, higher = more accurate
MONTE_CARLO_SIMS = 1500

# Mixed-strategy blending
BLEND_WIDTH = 0.08   # equity range over which actions are mixed near each threshold
                     # e.g. with BLEND_WIDTH=0.08 and POSTFLOP_RAISE_THRESHOLD=0.60,
                     # the bot raises 50% of the time at equity=0.64, 100% at equity=0.68+

# SPR (stack-to-pot ratio) adjustment
# When SPR > this value we raise threshold shifts up by SPR_RAISE_ADJUST
# (in deep spots, protect equity more carefully across multiple streets)
SPR_DEEP_THRESHOLD   = 10.0
SPR_RAISE_ADJUST     = 0.04
# ---------------------------------------------------------------------------


def _to_treys(card: Card) -> int:
    return TreysCard.new(card.to_treys())


def _build_deck() -> list[int]:
    ranks = "23456789TJQKA"
    suits = "shdc"
    return [TreysCard.new(r + s) for r in ranks for s in suits]


def monte_carlo_equity(
    hole_cards: list[Card],
    board_cards: list[Card],
    num_opponents: int,
    opponent_tier: str = "random",
    num_sims: int = MONTE_CARLO_SIMS,
) -> float:
    """
    Estimate win probability by simulation against a ranged opponent.

    opponent_tier: one of "premium", "tight", "medium", "wide", "random".
      Controls which hands the opponent is assumed to hold. A tighter tier
      means the opponent has stronger hands on average, which lowers our
      equity estimate and makes the bot play more conservatively.

    For each trial:
      1. Sample an opponent hand from the filtered range (no card conflicts).
      2. Fill remaining board cards randomly from the leftover deck.
      3. Evaluate all hands with treys. Ties count as half a win.

    Falls back to 0.5 if no valid trials could be run.
    """
    our_treys   = [_to_treys(c) for c in hole_cards]
    board_treys = [_to_treys(c) for c in board_cards]
    known       = set(our_treys + board_treys)

    # Opponent hand pool filtered against known cards
    opp_pool = get_combos(opponent_tier, known)
    if not opp_pool:
        opp_pool = get_combos("random", known)   # safety fallback

    # Remaining deck for board runouts (excludes known cards AND opponent hands
    # — we re-filter per trial below)
    full_deck = [c for c in _build_deck() if c not in known]

    wins  = 0
    valid = 0

    for _ in range(num_sims):
        # Pick a random opponent hand from the range
        opp_hand = list(random.choice(opp_pool))

        # Build the runout deck: full deck minus the opponent's two cards
        opp_set  = set(opp_hand)
        run_deck = [c for c in full_deck if c not in opp_set]

        need = 5 - len(board_cards)
        if len(run_deck) < need:
            continue

        random.shuffle(run_deck)
        sim_board = board_treys + run_deck[:need]

        our_score = _evaluator.evaluate(sim_board, our_treys)
        opp_score = _evaluator.evaluate(sim_board, opp_hand)

        # treys scores: lower is better (1 = Royal Flush, 7462 = worst)
        if our_score < opp_score:
            wins += 1
        elif our_score == opp_score:
            wins += 0.5   # chop

        valid += 1

    return wins / valid if valid else 0.5


# ---------- mixed-strategy helpers ----------

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _action_freqs(
    equity: float,
    strong_threshold: float,
    call_threshold: float,
    can_check: bool,
) -> tuple[float, float, float]:
    """
    Convert equity → (raise_freq, call_freq, fold_freq) summing to 1.0.

    Raise ramps from 0 to 1 across [strong_threshold, strong_threshold + BLEND_WIDTH].
    Fold  ramps from 0 to 1 across [call_threshold - BLEND_WIDTH, call_threshold].
    Fold is always 0 when can_check (free check dominates folding).
    Call fills whatever is left.
    """
    raise_freq = _clamp01((equity - strong_threshold) / BLEND_WIDTH)
    if can_check:
        fold_freq = 0.0
    else:
        fold_freq = _clamp01((call_threshold - equity) / BLEND_WIDTH)
    call_freq = max(0.0, 1.0 - raise_freq - fold_freq)
    total = raise_freq + call_freq + fold_freq
    return raise_freq / total, call_freq / total, fold_freq / total


# ---------- logging ----------

_W = 62  # log box width

def _log(lines: list[str]):
    """Print a bordered decision log block."""
    print("┌" + "─" * _W + "┐")
    for line in lines:
        print(f"│  {line:<{_W - 2}}│")
    print("└" + "─" * _W + "┘")


def _fmt_cards(cards) -> str:
    return "  ".join(str(c) for c in cards)


def _fmt_board(board) -> str:
    if len(board) == 0:   return "(no board)"
    if len(board) <= 3:   return _fmt_cards(board)
    return _fmt_cards(board[:3]) + "  |  " + _fmt_cards(board[3:])


# Compact combo formatting for range telemetry. Sort by rank desc, then suit,
# so the same two cards always render the same way in logs.
_RANK_ORDER = {r: i for i, r in enumerate("23456789TJQKA")}


def _combo_to_str(combo) -> str:
    cards = sorted(combo, key=lambda c: (-_RANK_ORDER.get(c.rank, 0), c.suit))
    return "".join(str(c) for c in cards)


def _format_top_combos(range_, n: int = 5) -> str:
    parts = [f"{_combo_to_str(c)}:{w:.2f}" for c, w in range_.top_n(n)]
    return " ".join(parts) if parts else "(empty)"


# ---------- decision ----------

def decide(state: GameState, opponent_tier: str = "random",
           context=None, decision_sink=None) -> tuple:
    """
    Evaluate the current game state and return an action.

    Parameters
      state          : current GameState snapshot
      opponent_tier  : 'premium' | 'tight' | 'medium' | 'wide' | 'random'
      context        : optional HandContext for cross-street awareness
      decision_sink  : optional callable(dict) — fed a structured record of
                       the decision (source, frequencies, roll, action, etc.)
                       so the session logger can persist it to JSONL

    Returns
      (action, amount) where action ∈ {'fold','check','call','raise'}.
    """
    bb  = state.big_blind if state.big_blind > 0 else 0.01

    # Effective pot reconstruction: pokernow's displayed pot often misses
    # current-street action during live betting rounds (we've seen pot=0
    # facing a preflop jam, and pot=preflop-only facing a flop bet/raise/
    # re-raise).  When we have a HandContext, use the per-street stack
    # snapshot to compute hero's contribution this street and add the
    # estimated current-street action back in.  Falls back gracefully to
    # the displayed pot when no context is available.
    hero_round_contrib = (context.hero_round_contribution(state.phase, state.my_stack)
                          if context is not None else 0.0)
    effective_pot = state.pot + 2 * hero_round_contrib + state.to_call

    # SPR and bet_fraction now use effective_pot, so misclassifications of
    # bet sizing tier (small/medium/large/overbet) and SPR-based threshold
    # adjustments are corrected too.
    spr = state.my_stack / effective_pot if effective_pot > 0 else 99.0
    source = "monte_carlo"
    log: list[str] = []

    # Tier blend (v2.4): take min(legacy, range) only when villain has shown
    # preflop aggression (3-bet+). In single-raised pots — where villain just
    # called our open — the range tracker collapses villain to a tight-looking
    # ~90 effective combos, which kills the solver's +25% exploit boost (it
    # only fires for wide/random tiers). The legacy HandTracker correctly
    # reads single-raised pots as `wide` from the start. So:
    #   - villain 3-bet (or beyond): take min — protects against the v2.1
    #     QcAc-style 4-bet pot leak where range tracking missed a chained raise
    #   - villain just called our open: trust the range tier — it correctly
    #     reflects which combos are in BB's defending range, but solver
    #     exploit logic still wants the wider tier label for HU dynamics.
    #
    # The bot-vs-bot 575-hand session showed avg air-bet freq drop to 2% with
    # the unconditional min; this conditional restores closer to the intended
    # ~32% on dry boards by re-enabling the exploit boost when applicable.
    _TIER_ORDER = ("premium", "tight", "medium", "wide", "random")
    legacy_tier = opponent_tier
    if context is not None:
        vr_for_tier = getattr(context, "villain_range", None)
        if vr_for_tier is not None and vr_for_tier.total() > 0:
            range_tier = vr_for_tier.to_tier()
            v_was_aggressive_pre = getattr(context, "villain_preflop_raises", 0) > 0
            try:
                if v_was_aggressive_pre:
                    final_tier = _TIER_ORDER[min(_TIER_ORDER.index(legacy_tier),
                                                 _TIER_ORDER.index(range_tier))]
                    blend_mode = "min"
                else:
                    final_tier = range_tier
                    blend_mode = "range"
            except ValueError:
                final_tier = range_tier
                blend_mode = "range"
            if final_tier != legacy_tier or range_tier != legacy_tier:
                log.append(f"  Tier blend ({blend_mode})  legacy={legacy_tier}  "
                           f"range={range_tier}  → {final_tier}")
            opponent_tier = final_tier

    # Cross-street context snapshot (used by heuristics + logged)
    ctx_summary = {}
    if context is not None:
        ctx_summary = {
            "pfa":                 context.preflop_aggressor,
            "hero_cbet_flop":      context.hero_cbet("flop"),
            "hero_cbet_turn":      context.hero_cbet("turn"),
            "double_barrel_spot":  context.is_double_barrel_spot(state.phase),
            "villain_check_called": context.villain_check_called_last_street(state.phase),
        }
        if state.phase != "preflop":
            log.append(f"  Ctx    PFA={ctx_summary['pfa']}"
                       f"  cbet(flop)={ctx_summary['hero_cbet_flop']}"
                       f"  cbet(turn)={ctx_summary['hero_cbet_turn']}"
                       f"  barrel_spot={ctx_summary['double_barrel_spot']}")

    # ── Header ───────────────────────────────────────────────────────────────
    hand_str  = _fmt_cards(state.hole_cards)
    board_str = _fmt_board(state.board_cards)
    log.append(f"DECISION  [{state.phase.upper()}]")
    log.append(f"  Hand   {hand_str}")
    log.append(f"  Board  {board_str}")
    log.append(f"  Pos    {state.position}  {'(IP)' if state.position in ('BTN','CO','HJ') else '(OOP)'}")
    log.append(f"  Stack  {state.my_stack:.2f}   Pot {state.pot:.2f}   "
               f"ToCall {state.to_call:.2f}   SPR {spr:.1f}")
    log.append(f"  Odds   pot_odds={state.pot_odds():.1%}   "
               f"can_check={state.can_check}")
    log.append(f"  Villain  tier={opponent_tier}   opponents={state.num_opponents}")

    # Range tracking telemetry — printed here for live debugging and also
    # captured into the JSONL decision record (see _emit) so post-session
    # review can replay the exact ranges the bot believed at decision time.
    range_telemetry: dict | None = None
    if context is not None:
        hr = getattr(context, "hero_range", None)
        vr = getattr(context, "villain_range", None)
        if hr is not None and vr is not None and hr.total() > 0 and vr.total() > 0:
            rvr_eq: float | None = None
            if state.phase != "preflop" and len(state.board_cards) >= 3:
                try:
                    rvr_eq = range_vs_range_equity(
                        hr, vr, state.board_cards, num_sims=600,
                    )
                except Exception:
                    rvr_eq = None

            log.append(f"  Ranges   hero={hr.num_combos()} combos  "
                       f"villain={vr.num_combos()} combos  ({hr.to_tier()}/{vr.to_tier()})")
            log.append(f"  HeroTop  {_format_top_combos(hr, 5)}")
            log.append(f"  VilTop   {_format_top_combos(vr, 5)}")
            if rvr_eq is not None:
                log.append(f"  RangeEq  hero_range vs villain_range = {rvr_eq:.1%}")

            range_telemetry = {
                "hero_combos":           hr.num_combos(),
                "hero_tier":             hr.to_tier(),
                "hero_total_weight":     round(hr.total(), 4),
                "villain_combos":        vr.num_combos(),
                "villain_tier":          vr.to_tier(),
                "villain_total_weight":  round(vr.total(), 4),
                "range_vs_range_equity": (round(rvr_eq, 4) if rvr_eq is not None else None),
                "hero_top":    [[_combo_to_str(c), round(w, 4)] for c, w in hr.top_n(20)],
                "villain_top": [[_combo_to_str(c), round(w, 4)] for c, w in vr.top_n(20)],
            }
    log.append("─" * (_W - 2))

    # ── Postflop: try GTO flop lookup ────────────────────────────────────────
    if state.phase == "flop":
        ftex      = flop_texture(state.board_cards)
        hclass    = classify(state.hole_cards, state.board_cards)
        spr_b     = "low" if spr < 4 else ("high" if spr >= 11 else "medium")
        facing_b  = state.to_call > 0
        log.append(f"  [flop]  texture={ftex}")
        log.append(f"          hand_class={hclass}   spr_bucket={spr_b}   facing_bet={facing_b}")

        lut_freqs = _lut.query(state, state.hole_cards, opponent_tier)
        if lut_freqs is not None:
            raise_freq, call_freq, fold_freq = lut_freqs
            log.append(f"  Solver  HIT  key=({ftex}, {spr_b}, {opponent_tier}, {facing_b})")
            log.append(f"          raw  R={raise_freq:.0%}  C={call_freq:.0%}  F={fold_freq:.0%}")
            # Exploit: GTO solver freqs assume a balanced defender. Vs loose-passive
            # opponents (tier wide/random) c-bet ranges should widen — they fold too
            # much postflop. Take ~40% of the call/check headroom into raise, capped
            # at +25 % so we never blow up the GTO baseline entirely.
            if not facing_b and opponent_tier in ("wide", "random"):
                boost = min(0.25, (1.0 - raise_freq) * 0.4)
                if boost > 0:
                    raise_freq += boost
                    call_freq = max(0.0, call_freq - boost)
                    log.append(f"          exploit vs {opponent_tier}: +{boost:.0%} raise"
                               f"  →  R={raise_freq:.0%}  C={call_freq:.0%}")
            if state.can_check:
                call_freq = 1.0 - raise_freq   # fold → check; no renorm
                fold_freq = 0.0
                log.append(f"          can_check → fold→check  R={raise_freq:.0%}  C={call_freq:.0%}")
            source = "solver"
        else:
            log.append(f"  Solver  MISS  → falling back to monte_carlo")

    # ── Preflop: position-aware GTO range table ───────────────────────────────
    if source == "monte_carlo" and state.phase == "preflop":
        hkey         = preflop_ranges.hand_key(state.hole_cards)
        facing_raise = state.to_call > state.big_blind
        num_players  = state.num_opponents + 1
        raise_freq, call_freq, fold_freq = preflop_ranges.lookup(
            state.hole_cards, state.position, facing_raise,
            num_players=num_players,
        )
        log.append(f"  [preflop]  hand={hkey}   facing_raise={facing_raise}   players={num_players}")
        log.append(f"  Range table  R={raise_freq:.0%}  C={call_freq:.0%}  F={fold_freq:.0%}")
        # Preflop jam guard: when villain shoves or makes a very large raise,
        # tighten the calling range dramatically. The static range tables are
        # calibrated for normal 2.5–3.5x opens — they over-defend vs jams.
        # Severity ramps from 0.5 (at 20×BB) to 0.9 (at 60+ ×BB).  Raise freq
        # is preserved so 3-bet jams with QQ+/AK still go in.
        if facing_raise and state.big_blind > 0 and (
                state.to_call >= state.big_blind * 20
                or (state.my_stack > 0 and state.to_call >= state.my_stack * 0.5)):
            ratio    = state.to_call / state.big_blind
            severity = min(0.9, 0.5 + max(0.0, ratio - 20) / 80 * 0.4)
            folded   = call_freq * severity
            call_freq -= folded
            fold_freq += folded
            log.append(f"  Jam guard  toCall={ratio:.0f}×BB  severity={severity:.0%}"
                       f"  →  R={raise_freq:.0%}  C={call_freq:.0%}  F={fold_freq:.0%}")
        if state.can_check:
            call_freq = 1.0 - raise_freq   # fold → check; no renorm
            fold_freq = 0.0
            log.append(f"  BB option (can check) → fold→check  R={raise_freq:.0%}  C={call_freq:.0%}")
        source = "preflop_gto"

    # ── Turn: pseudo-GTO heuristic ────────────────────────────────────────────
    if source == "monte_carlo" and state.phase == "turn":
        hclass       = classify(state.hole_cards, state.board_cards)
        ttex         = turn_card_texture(state.board_cards)
        turn_str     = str(state.board_cards[3])
        facing_b     = state.to_call > 0 or (not state.can_check and state.pot > 0)
        bet_frac     = (round(state.to_call / effective_pot, 2) if state.to_call > 0 and effective_pot > 0
                        else (2.0 if facing_b else 0.0))
        bet_tier_s   = (("small" if bet_frac <= 0.35 else "medium" if bet_frac <= 0.75
                         else "large" if bet_frac <= 1.15 else "overbet") if facing_b else "-")
        is_ip        = state.position in ("BTN", "CO", "HJ")
        spr_tag      = "low" if spr < 4 else ("high" if spr > 10 else "medium")

        if facing_b and state.to_call == 0:
            log.append(f"  [guard] to_call=0 with no check — inferring jam (bet_frac=2.0)")
        log.append(f"  [turn]  card={turn_str}  texture={ttex}")
        log.append(f"          hand_class={hclass}   facing_bet={facing_b}"
                   + (f"  bet={bet_frac:.2f}×pot ({bet_tier_s})" if facing_b else ""))
        log.append(f"          IP={is_ip}   SPR={spr:.1f}({spr_tag})   villain={opponent_tier}")

        raise_freq, call_freq, fold_freq = turn_heuristic.query(
            hole_cards   = state.hole_cards,
            board_cards  = state.board_cards,
            position     = state.position,
            villain_tier = opponent_tier,
            facing_bet   = facing_b,
            spr          = spr,
            bet_fraction = bet_frac,
            context      = context,
        )
        log.append(f"  Heuristic  R={raise_freq:.0%}  C={call_freq:.0%}  F={fold_freq:.0%}")
        if state.can_check:
            call_freq = 1.0 - raise_freq   # fold → check; no renorm
            fold_freq = 0.0
            log.append(f"  can_check → fold→check  R={raise_freq:.0%}  C={call_freq:.0%}")
        source = "turn_heuristic"

    # ── River: pseudo-GTO heuristic ──────────────────────────────────────────
    if source == "monte_carlo" and state.phase == "river":
        hclass       = classify(state.hole_cards, state.board_cards)
        rtex         = river_card_texture(state.board_cards)
        river_str    = str(state.board_cards[4]) if len(state.board_cards) >= 5 else "?"
        facing_b     = state.to_call > 0 or (not state.can_check and state.pot > 0)
        bet_frac     = (round(state.to_call / effective_pot, 2) if state.to_call > 0 and effective_pot > 0
                        else (2.0 if facing_b else 0.0))
        bet_tier_s   = (("small" if bet_frac <= 0.35 else "medium" if bet_frac <= 0.75
                         else "large" if bet_frac <= 1.15 else "overbet") if facing_b else "-")
        is_ip        = state.position in ("BTN", "CO", "HJ")
        spr_tag      = "low" if spr < 4 else ("high" if spr > 10 else "medium")

        if facing_b and state.to_call == 0:
            log.append(f"  [guard] to_call=0 with no check — inferring jam (bet_frac=2.0)")
        log.append(f"  [river]  card={river_str}  texture={rtex}")
        log.append(f"          hand_class={hclass}   facing_bet={facing_b}"
                   + (f"  bet={bet_frac:.2f}×pot ({bet_tier_s})" if facing_b else ""))
        log.append(f"          IP={is_ip}   SPR={spr:.1f}({spr_tag})   villain={opponent_tier}")

        raise_freq, call_freq, fold_freq = river_heuristic.query(
            hole_cards   = state.hole_cards,
            board_cards  = state.board_cards,
            position     = state.position,
            villain_tier = opponent_tier,
            facing_bet   = facing_b,
            spr          = spr,
            bet_fraction = bet_frac,
            context      = context,
        )
        log.append(f"  Heuristic  R={raise_freq:.0%}  C={call_freq:.0%}  F={fold_freq:.0%}")
        if state.can_check:
            call_freq = 1.0 - raise_freq   # fold → check; no renorm
            fold_freq = 0.0
            log.append(f"  can_check → fold→check  R={raise_freq:.0%}  C={call_freq:.0%}")
        source = "river_heuristic"

    # ── Postflop fallback: Monte Carlo ────────────────────────────────────────
    if source == "monte_carlo":
        # Prefer range-aware equity when the context has a populated villain
        # range (Phase 2: combo-level range tracking). Falls back to the
        # tier-based MC sampler when ranges aren't available, so tests / no-
        # context callers still work the same way they did before.
        used_range = False
        if context is not None and getattr(context, "villain_range", None) is not None \
                and context.villain_range.total() > 0:
            log.append(f"  [monte_carlo]  running {MONTE_CARLO_SIMS} sims  "
                       f"vs villain_range ({context.villain_range.num_combos()} combos)")
            equity = hand_vs_range_equity(
                state.hole_cards, context.villain_range, state.board_cards,
                num_sims=MONTE_CARLO_SIMS,
            )
            used_range = True
        else:
            log.append(f"  [monte_carlo]  running {MONTE_CARLO_SIMS} sims  "
                       f"vs tier={opponent_tier}")
            equity = monte_carlo_equity(
                state.hole_cards, state.board_cards,
                state.num_opponents, opponent_tier=opponent_tier,
            )
        strong_threshold = POSTFLOP_RAISE_THRESHOLD
        call_threshold   = state.pot_odds() + POSTFLOP_CALL_EDGE
        spr_adj_applied  = ""
        if state.pot > 0 and spr > SPR_DEEP_THRESHOLD:
            strong_threshold += SPR_RAISE_ADJUST
            spr_adj_applied   = f"  (deep SPR +{SPR_RAISE_ADJUST} raise thresh)"

        raise_freq, call_freq, fold_freq = _action_freqs(
            equity, strong_threshold, call_threshold, state.can_check
        )
        eq_label = "range_eq" if used_range else "tier_eq"
        log.append(f"  {eq_label}={equity:.1%}   raise_thresh={strong_threshold:.2f}"
                   f"   call_thresh={call_threshold:.2f}{spr_adj_applied}")

    # ── Range-advantage bluff/value modulation (postflop) ────────────────────
    # When our range outperforms villain's on this board, our bluff candidates
    # become more profitable to fire (villain has fewer made hands, our bluffs
    # have more fold equity). Conversely, on boards where villain has range
    # advantage, bluffing into a stronger range is a leak.
    #
    # This adjustment ONLY applies to bluff-class hands (air, weak_draw,
    # draw). Value hands keep their source-specific freqs — we don't want to
    # under-bet aces just because we're on a low connected board.
    if (state.phase != "preflop"
            and range_telemetry is not None
            and range_telemetry.get("range_vs_range_equity") is not None):
        rvr = range_telemetry["range_vs_range_equity"]
        try:
            _hclass_mod = classify(state.hole_cards, state.board_cards)
        except Exception:
            _hclass_mod = "?"
        # Bluff modulation only fires when we're betting into a checked-to-us
        # spot. When facing a bet, villain's action itself signals a tightened
        # range — adding +rvr-adj on top is the wrong direction (it caused
        # spurious bluff-raises in 4-bet pots like the 2026-04-27 QcAc hand
        # where AQ raised a pot-size turn bet on KT82 with weak_draw).
        if _hclass_mod in {"air", "weak_draw", "draw"} and state.to_call == 0:
            # ±15% raise-freq swing across a 30-pp rvr range centered at 50%.
            # rvr=0.65 → +0.045, rvr=0.35 → −0.045, capped at ±0.15.
            adj = max(-0.15, min(0.15, (rvr - 0.5) * 0.30))
            old_raise = raise_freq
            raise_freq = max(0.0, min(1.0, raise_freq + adj))
            delta = raise_freq - old_raise
            if delta > 0:
                # Pull from call+fold proportionally so we don't fold more
                # just because we're bluffing more.
                source_pool = call_freq + fold_freq
                if source_pool > 0:
                    take = min(delta, source_pool)
                    call_freq -= take * (call_freq / source_pool)
                    fold_freq -= take * (fold_freq / source_pool)
            elif delta < 0:
                # Less bluffing → those frequencies become checks/calls,
                # not folds. Push the difference into call.
                call_freq -= delta   # delta is negative → this is +|delta|
            if abs(adj) > 0.005:
                log.append(f"  Range mod  rvr={rvr:.0%}  hclass={_hclass_mod}  "
                           f"adj={adj:+.0%}  →  R={raise_freq:.0%} "
                           f"C={call_freq:.0%} F={fold_freq:.0%}")

    # ── Big-call sanity check ────────────────────────────────────────────────
    # Source tables (solver / heuristic) can suggest calling marginal hands at
    # surprising frequencies in spots where actual hand-vs-range equity is
    # awful. Validate the call against the real equity of *this specific
    # holding* vs villain's narrowed range whenever we're committing real
    # money — pot-size+ bets, river calls (no implied odds), or stack-deep
    # commits. This is the user's "still consider actual holding for big
    # bets" rule — range-advantage drives bluffs, the actual hand has the
    # final say on big calls.
    #
    # Note on threshold: state.to_call / effective_pot caps at 1.0 even for
    # jam-overbets (effective_pot includes to_call), so 0.50 corresponds to
    # a pot-size bet by traditional reckoning. Rivers always check.
    _bigcall_size = (state.to_call > 0 and effective_pot > 0
                     and state.to_call >= 0.50 * effective_pot)
    _bigcall_river = state.phase == "river" and state.to_call > 0
    _bigcall_commit = (state.to_call > 0 and state.my_stack > 0
                       and state.to_call >= 0.30 * state.my_stack)
    if (state.phase != "preflop"
            and (_bigcall_size or _bigcall_river or _bigcall_commit)
            and context is not None
            and getattr(context, "villain_range", None) is not None
            and context.villain_range.total() > 0
            and call_freq > 0.05):
        try:
            actual_eq = hand_vs_range_equity(
                state.hole_cards, context.villain_range, state.board_cards,
                num_sims=1500,
            )
        except Exception:
            actual_eq = None
        if actual_eq is not None:
            pot_odds_frac = state.to_call / (state.to_call + effective_pot)
            required = pot_odds_frac + 0.03
            if actual_eq < required:
                deficit    = required - actual_eq
                fold_boost = min(0.30, deficit * 1.5)
                transferred = min(call_freq, fold_boost)
                call_freq -= transferred
                fold_freq += transferred
                log.append(f"  Big-call check  to_call={state.to_call/effective_pot:.0%}pot  "
                           f"actual_eq={actual_eq:.0%} req={required:.0%}  "
                           f"→ +{transferred:.0%} fold")

    # ── Postflop re-raise guard ───────────────────────────────────────────────
    # If we already raised this street and now face another bet, villain has
    # check-raised or 3-bet us. The source-specific freqs (solver / heuristic /
    # MC) don't know about within-street action history — they'll happily
    # repeat their original "raise this spot" answer and we'll spew chips.
    # Tighten dramatically based on hand strength.
    _re_raise_spot = (
        state.phase != "preflop"
        and state.to_call > 0
        and context is not None
        and context.streets.get(state.phase) is not None
        and context.streets[state.phase].hero_bet
    )
    if _re_raise_spot:
        _hclass = classify(state.hole_cards, state.board_cards)
        if _hclass in {"monster", "full_house", "flush", "straight", "set"}:
            pass   # strong — let the original freqs play out
        elif _hclass in {"two_pair", "overpair", "top_pair_top", "trips"}:
            # Don't 5-bet bluff with strong-ish made hands at low SPR — but
            # don't invent fold% either. Transfer raise → call and let the
            # source's F% speak for itself.
            call_freq += raise_freq
            raise_freq = 0.0
            log.append(f"  Re-raise guard ({_hclass})  → R=0%  C={call_freq:.0%}  F={fold_freq:.0%}")
        else:
            raise_freq = 0.0
            call_freq  = call_freq * 0.3
            fold_freq  = max(0.0, 1.0 - call_freq)
            log.append(f"  Re-raise guard ({_hclass})  → R=0%  C={call_freq:.0%}  F={fold_freq:.0%}")

    # ── No-raise guard ────────────────────────────────────────────────────────
    # Raise is counter-productive when calling already commits most of our stack:
    #   (a) calling puts us all-in (to_call >= stack)
    #   (b) calling uses 60 %+ of remaining stack (near-jam — re-raise is marginal)
    # The heuristic's overbet-tier adjustment already handles large bets vs a deep
    # stack; applying this guard on top would double-count the same signal.
    _is_jam       = state.to_call > 0 and state.to_call >= state.my_stack
    _is_committed = (state.to_call > 0 and state.my_stack > 0
                     and state.to_call > state.my_stack * 0.6)
    if _is_jam or _is_committed:
        # When stack <= to_call, "raising" IS calling all-in — preserve the
        # strategy's commit-chips intent by transferring raise_freq into
        # call_freq instead of evaporating it.
        call_freq    += raise_freq
        raise_freq    = 0.0

        # Pot-odds calc using the reconstructed effective_pot (computed at
        # the top of decide() from HandContext stack snapshots).  This
        # avoids the misread-pot blowup that previously returned pot_odds
        # of 67-100% when the scraper's pot reading was stuck on
        # closed-streets-only.
        pot_odds_frac = state.to_call / (state.to_call + effective_pot)

        total = call_freq + fold_freq
        if total > 0:
            call_freq /= total
            fold_freq /= total
        else:
            fold_freq = 1.0
        odds_adj  = (0.5 - pot_odds_frac) * 0.4   # ±0.20 max; pos = lean call
        call_freq = max(0.0, min(1.0, call_freq + odds_adj))
        fold_freq = max(0.0, 1.0 - call_freq)
        reason = "jam" if _is_jam else "pot-committed"
        log.append(f"  No-raise ({reason})  pot_odds={pot_odds_frac:.0%}  "
                   f"adj={odds_adj:+.0%}  → C={call_freq:.0%}  F={fold_freq:.0%}")

    # ── Tiny-bet override ─────────────────────────────────────────────────────
    # When pot odds are absurdly favorable (a 0.10 bet into 4.80 = 2%), folding
    # is a leak with literally any two cards.  Cap fold_freq at 5% — treat as a
    # check-equivalent.  Triggers regardless of source (solver / heuristic / MC)
    # so any bet sizing this small is handled cleanly.
    if state.to_call > 0 and effective_pot > 0:
        _po = state.to_call / (state.to_call + effective_pot)
        if _po <= 0.10 and fold_freq > 0.05:
            transferred = fold_freq - 0.05
            call_freq  += transferred
            fold_freq   = 0.05
            log.append(f"  Tiny bet override  pot_odds={_po:.0%}"
                       f"  →  R={raise_freq:.0%}  C={call_freq:.0%}  F={fold_freq:.0%}")

    # ── Frequencies summary ───────────────────────────────────────────────────
    log.append("─" * (_W - 2))
    log.append(f"  Source  {source}")
    log.append(f"  Freq    RAISE={raise_freq:.0%}   CALL={call_freq:.0%}   FOLD={fold_freq:.0%}")

    # ── Random roll ───────────────────────────────────────────────────────────
    roll = random.random()
    r_thresh = raise_freq
    c_thresh = raise_freq + call_freq
    if roll < r_thresh:
        roll_result = f"RAISE  ({roll:.4f} < {r_thresh:.4f})"
    elif roll < c_thresh:
        roll_result = f"{'CHECK' if state.can_check else 'CALL'}  ({roll:.4f} in [{r_thresh:.4f}, {c_thresh:.4f}))"
    else:
        roll_result = f"{'CHECK' if state.can_check else 'FOLD'}  ({roll:.4f} >= {c_thresh:.4f})"
    log.append(f"  Roll    {roll:.4f}  →  {roll_result}")

    # ── Emit structured record + return action ────────────────────────────────
    def _emit(action: str, amount: float):
        if decision_sink is None:
            return
        try:
            hclass_out = (classify(state.hole_cards, state.board_cards)
                          if state.phase != "preflop" else "preflop")
        except Exception:
            hclass_out = "?"
        decision_sink({
            "hand_id":       getattr(context, "hand_id", None),
            "phase":         state.phase,
            "position":      state.position,
            "hole":          " ".join(str(c) for c in state.hole_cards),
            "board":         " ".join(str(c) for c in state.board_cards),
            "pot":           round(state.pot, 2),
            "to_call":       round(state.to_call, 2),
            "my_stack":      round(state.my_stack, 2),
            "bb":            round(bb, 2),
            "spr":           round(spr, 2),
            "can_check":     state.can_check,
            "villain_tier":  opponent_tier,
            "num_opponents": state.num_opponents,
            "hand_class":    hclass_out,
            "source":        source,
            "raise_freq":    round(raise_freq, 3),
            "call_freq":     round(call_freq, 3),
            "fold_freq":     round(fold_freq, 3),
            "roll":          round(roll, 4),
            "action":        action,
            "amount":        round(amount, 2),
            "context":       ctx_summary,
            "ranges":        range_telemetry,
        })

    if roll < raise_freq:
        if state.phase == "preflop":
            facing_raise = state.to_call > state.big_blind
            raise_to, sizing_log = pick_preflop_size(
                state.position, facing_raise, state.to_call, bb
            )
        else:
            _hc = classify(state.hole_cards, state.board_cards)
            bet_frac, sizing_log = pick_bet_fraction(
                _hc, state.board_cards, state.to_call > 0
            )
            if state.to_call > 0:
                pot_after_call = state.pot + state.to_call
                raise_to = round(state.to_call + bet_frac * pot_after_call, 2)
            else:
                raise_to = round(state.pot * bet_frac, 2)
        log.extend(sizing_log)
        min_legal_raise = state.to_call * 2 if state.to_call > 0 else bb * 2
        raise_to = max(raise_to, min_legal_raise)
        raise_to = round(min(raise_to, state.my_stack), 2)
        log.append(f"  Action  ► RAISE  to {raise_to:.2f}")
        _log(log)
        _emit("raise", raise_to)
        return ("raise", raise_to)

    if roll < raise_freq + call_freq:
        if state.can_check:
            log.append(f"  Action  ► CHECK")
            _log(log)
            _emit("check", 0)
            return ("check", 0)
        log.append(f"  Action  ► CALL  {state.to_call:.2f}")
        _log(log)
        _emit("call", state.to_call)
        return ("call", state.to_call)

    if state.can_check:
        log.append(f"  Action  ► CHECK  (folded region → free check)")
        _log(log)
        _emit("check", 0)
        return ("check", 0)
    log.append(f"  Action  ► FOLD")
    _log(log)
    _emit("fold", 0)
    return ("fold", 0)
