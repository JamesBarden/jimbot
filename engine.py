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

from state import Card, GameState
from ranges import get_combos
from solver_lookup import SolverLookup
from hand_classifier import classify, board_texture as flop_texture, turn_card_texture, river_card_texture
import preflop_ranges
import turn_heuristic
import river_heuristic
from bet_sizing import pick_bet_fraction, pick_preflop_size

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


# ---------- decision ----------

def decide(state: GameState, opponent_tier: str = "random") -> tuple[str, int]:
    """
    Evaluate the current game state and return an action.

    Returns one of:
      ('fold',  0)
      ('check', 0)
      ('call',  to_call_amount)
      ('raise', raise_to_amount)
    """
    bb  = state.big_blind if state.big_blind > 0 else 0.01
    spr = state.my_stack / state.pot if state.pot > 0 else 99.0
    source = "monte_carlo"
    log: list[str] = []

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
        bet_frac     = (round(state.to_call / state.pot, 2) if state.to_call > 0 and state.pot > 0
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
        bet_frac     = (round(state.to_call / state.pot, 2) if state.to_call > 0 and state.pot > 0
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
        )
        log.append(f"  Heuristic  R={raise_freq:.0%}  C={call_freq:.0%}  F={fold_freq:.0%}")
        if state.can_check:
            call_freq = 1.0 - raise_freq   # fold → check; no renorm
            fold_freq = 0.0
            log.append(f"  can_check → fold→check  R={raise_freq:.0%}  C={call_freq:.0%}")
        source = "river_heuristic"

    # ── Postflop fallback: Monte Carlo ────────────────────────────────────────
    if source == "monte_carlo":
        log.append(f"  [monte_carlo]  running {MONTE_CARLO_SIMS} sims  "
                   f"vs tier={opponent_tier}")
        equity           = monte_carlo_equity(
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
        log.append(f"  equity={equity:.1%}   raise_thresh={strong_threshold:.2f}"
                   f"   call_thresh={call_threshold:.2f}{spr_adj_applied}")

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
        raise_freq    = 0.0
        pot_odds_frac = state.to_call / (state.pot + state.to_call)
        total         = call_freq + fold_freq
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

    # ── Compute raise amount ──────────────────────────────────────────────────
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
        return ("raise", raise_to)

    if roll < raise_freq + call_freq:
        if state.can_check:
            log.append(f"  Action  ► CHECK")
            _log(log)
            return ("check", 0)
        log.append(f"  Action  ► CALL  {state.to_call:.2f}")
        _log(log)
        return ("call", state.to_call)

    if state.can_check:
        log.append(f"  Action  ► CHECK  (folded region → free check)")
        _log(log)
        return ("check", 0)
    log.append(f"  Action  ► FOLD")
    _log(log)
    return ("fold", 0)
